import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from responsegenerator.judgeai_metrics import JUDGE_METRIC_NAMES, ensure_judge_metrics
from responsegenerator.llm_client import LLMServiceError, call_configured_llm
from responsegenerator.models import (
    AdminPonderSec,
    AvaliacaoFormulario,
    AvaliacaoJuiz,
    AvaliacaoPublicaLLM,
    Avaliador,
    Formulario,
    LLM,
    LLMPublica,
    Metrica,
    PerguntaPublica,
    Questao,
    Resposta,
    RespostaPublica,
)
from responsegenerator.views import ADMIN_SESSION_KEY, _parse_judgeai_result


def judge_payload():
    # A ordem é intencionalmente diferente e Acurácia vem sem acento para testar o parser.
    return json.dumps({
        "notas": [
            {"metrica": "Clareza", "nota": 4, "justificativa": "A orientação usa linguagem compreensível e organiza os passos sem ambiguidades."},
            {"metrica": "Acuracia", "nota": 5, "justificativa": "As recomendações técnicas apresentadas estão corretas e não contêm afirmações factuais enganosas."},
            {"metrica": "Diretividade", "nota": 3, "justificativa": "A resposta atende à pergunta, embora inclua uma explicação secundária que reduz um pouco o foco."},
            {"metrica": "Completude", "nota": 4, "justificativa": "A resposta cobre os controles essenciais e deixa de mencionar apenas uma medida complementar."},
        ],
        "justificativa": "A resposta é tecnicamente sólida, mas pode ser mais direta.",
    }, ensure_ascii=False)


class PublicChatCrossEvaluationTests(TestCase):
    def setUp(self):
        self.metrics = ensure_judge_metrics(None)
        self.llm_a = LLMPublica.objects.create(
            nome="modelo-a", descricao="OpenAI", api_key="key-a", ativo=True,
        )
        self.llm_b = LLMPublica.objects.create(
            nome="modelo-b", descricao="OpenAI", api_key="key-b", ativo=True,
        )

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    def test_public_chat_generates_four_correctly_mapped_scores(self, mocked_call):
        mocked_call.side_effect = lambda llm, prompt: (
            judge_payload()
            if "atuando como juiz no chat público" in prompt
            else f"Resposta pública de {llm.nome}"
        )

        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({"pergunta": "Como evitar phishing?", "modelo_id": self.llm_a.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["avaliacao_cruzada"]["status"], "ok")
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 4)
        self.assertEqual(len(payload["tabela_avaliacao_cruzada"]), 4)
        saved = {
            item.metrica.nome: (item.avaliacao_quanti, item.avaliacao_quali)
            for item in AvaliacaoPublicaLLM.objects.select_related("metrica")
        }
        self.assertEqual(saved["Completude"][0], 4)
        self.assertEqual(saved["Acurácia"][0], 5)
        self.assertEqual(saved["Diretividade"][0], 3)
        self.assertEqual(saved["Clareza"][0], 4)
        self.assertIn("linguagem compreensível", saved["Clareza"][1])
        self.assertNotIn("afirmações técnicas", saved["Clareza"][1])
        self.assertEqual(payload["respostas"][0]["avaliacao"]["media_geral"], 4)

        answer_calls = [
            call for call in mocked_call.call_args_list
            if "atuando como juiz no chat público" not in call.args[1]
        ]
        self.assertEqual(len(answer_calls), 1)
        self.assertEqual(answer_calls[0].args[0].id, self.llm_a.id)

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    def test_same_model_configuration_cannot_judge_its_own_answer(self, mocked_call):
        duplicate = LLMPublica.objects.create(
            nome=self.llm_a.nome, descricao="OpenAI", api_key="another-key", ativo=True,
        )
        mocked_call.side_effect = lambda llm, prompt: judge_payload() if "atuando como juiz" in prompt else "Resposta"

        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({"pergunta": "Como usar MFA?", "modelo_id": self.llm_a.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        judge_ids = {
            item.juiz_id for item in AvaliacaoPublicaLLM.objects.all()
        }
        self.assertEqual(judge_ids, {self.llm_b.id})
        self.assertNotIn(duplicate.id, judge_ids)

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    def test_public_chat_exposes_provider_error_and_persists_failure(self, mocked_call):
        mocked_call.side_effect = LLMServiceError("A cota do provedor foi atingida.", code="quota_exceeded")

        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({"pergunta": "Como evitar phishing?", "modelo_id": self.llm_a.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["status"], "erro")
        self.assertIn("cota", response.json()["mensagem"].lower())
        failed = RespostaPublica.objects.get()
        self.assertFalse(failed.ok)
        self.assertIn("cota", failed.conteudo_resposta.lower())

    @mock.patch("responsegenerator.views._judgeai_stream_configured_llm")
    def test_public_chat_stream_emits_visible_error_event(self, mocked_stream):
        mocked_stream.side_effect = LLMServiceError("O provedor demorou além do tempo limite.", code="timeout")

        response = self.client.post(
            reverse("usuario_final_chat_stream_api"),
            data=json.dumps({"pergunta": "Como proteger a conta?", "modelo_id": self.llm_a.id}),
            content_type="application/json",
        )
        events = [
            json.loads(line)
            for line in b"".join(response.streaming_content).decode().splitlines()
        ]
        self.assertEqual([event["tipo"] for event in events], ["inicio", "erro"])
        self.assertIn("tempo limite", events[-1]["mensagem"].lower())
        self.assertFalse(RespostaPublica.objects.get().ok)

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    @mock.patch("responsegenerator.views._judgeai_stream_configured_llm")
    def test_public_chat_streams_selected_model_and_evaluates_with_other_model(self, mocked_stream, mocked_judge):
        mocked_stream.return_value = iter(["Use autenticação ", "em dois fatores."])
        mocked_judge.return_value = judge_payload()

        response = self.client.post(
            reverse("usuario_final_chat_stream_api"),
            data=json.dumps({"pergunta": "Como proteger minha conta?", "modelo_id": self.llm_a.id}),
            content_type="application/json",
        )
        events = [
            json.loads(line)
            for line in b"".join(response.streaming_content).decode().splitlines()
        ]

        self.assertEqual(
            [event["tipo"] for event in events],
            ["inicio", "trecho", "trecho", "resposta_concluida", "concluido"],
        )
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 4)
        self.assertEqual(AvaliacaoPublicaLLM.objects.values("juiz_id").distinct().get()["juiz_id"], self.llm_b.id)

    def test_public_chat_validates_model_selection(self):
        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({"pergunta": "Como evitar phishing?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PerguntaPublica.objects.count(), 0)


class JudgeAIParserAndResearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pesquisador", password="senha-segura")
        self.client.force_login(self.user)
        self.metrics = ensure_judge_metrics(self.user)

    def test_parser_returns_official_order_and_correct_justifications(self):
        scores, general = _parse_judgeai_result(judge_payload(), self.metrics)
        self.assertEqual([item["metrica"] for item in scores], list(JUDGE_METRIC_NAMES))
        self.assertEqual([item["nota"] for item in scores], [4, 5, 3, 4])
        self.assertIn("controles essenciais", scores[0]["justificativa"])
        self.assertIn("recomendações técnicas", scores[1]["justificativa"])
        self.assertIn("explicação secundária", scores[2]["justificativa"])
        self.assertIn("linguagem compreensível", scores[3]["justificativa"])
        self.assertIn("tecnicamente sólida", general)

    def test_parser_rejects_unknown_missing_duplicate_and_out_of_range_metrics(self):
        cases = [
            {"notas": [{"metrica": "Fidelidade", "nota": 4, "justificativa": "Não é uma métrica válida."}]},
            {"notas": json.loads(judge_payload())["notas"][:-1]},
            {"notas": json.loads(judge_payload())["notas"] + [json.loads(judge_payload())["notas"][0]]},
            {"notas": [{**item, "nota": 0} if item["metrica"] == "Clareza" else item for item in json.loads(judge_payload())["notas"]]},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _parse_judgeai_result(json.dumps(payload, ensure_ascii=False), self.metrics)

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm", return_value=judge_payload())
    def test_research_judgeai_never_self_evaluates_and_saves_four_metrics(self, mocked_call):
        llm_a = LLM.objects.create(usuario=self.user, nome="model-a", descricao="Groq", api_key="a")
        llm_b = LLM.objects.create(usuario=self.user, nome="model-b", descricao="Gemini", api_key="b")
        question = Questao.objects.create(usuario=self.user, conteudo="Como evitar phishing?")
        answer_a = Resposta.objects.create(questao=question, llm=llm_a, conteudo_resposta="Resposta A")
        answer_b = Resposta.objects.create(questao=question, llm=llm_b, conteudo_resposta="Resposta B")

        response = self.client.post(
            reverse("juizes_executar_avaliacao"),
            data=json.dumps({"questao_ids": [question.id], "juiz_ids": [llm_a.id, llm_b.id]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["notas_total"], 8)
        self.assertEqual(AvaliacaoJuiz.objects.count(), 8)
        self.assertFalse(AvaliacaoJuiz.objects.filter(resposta=answer_a, juiz=llm_a).exists())
        self.assertFalse(AvaliacaoJuiz.objects.filter(resposta=answer_b, juiz=llm_b).exists())
        for result in payload["resultados"]:
            self.assertEqual([item["metrica"] for item in result["notas"]], list(JUDGE_METRIC_NAMES))
        self.assertEqual(mocked_call.call_count, 2)

    def test_database_rejects_judge_score_outside_one_to_five(self):
        llm = LLM.objects.create(usuario=self.user, nome="model-a", descricao="Groq", api_key="a")
        question = Questao.objects.create(usuario=self.user, conteudo="Pergunta")
        answer = Resposta.objects.create(questao=question, llm=llm, conteudo_resposta="Resposta")
        with self.assertRaises(IntegrityError), transaction.atomic():
            AvaliacaoJuiz.objects.create(
                usuario=self.user,
                juiz=llm,
                resposta=answer,
                metrica=self.metrics[0],
                avaliacao_quanti=0,
            )


class ProviderClientTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="provider-user", password="senha")

    @mock.patch("responsegenerator.llm_client.genai_types")
    @mock.patch("responsegenerator.llm_client.genai")
    def test_gemini_reloads_replaced_key_and_uses_timeout(self, mocked_genai, mocked_types):
        llm = LLM.objects.create(
            usuario=self.user, nome="gemini-2.5-flash", descricao="Gemini", api_key="old-key",
        )
        LLM.objects.filter(pk=llm.pk).update(api_key="new-key")
        http_options = object()
        mocked_types.HttpOptions.return_value = http_options
        client = mocked_genai.Client.return_value
        client.models.generate_content.return_value = SimpleNamespace(text="Resposta Gemini")

        result = call_configured_llm(llm, "Pergunta")

        self.assertEqual(result, "Resposta Gemini")
        mocked_genai.Client.assert_called_once_with(api_key="new-key", http_options=http_options)
        mocked_types.HttpOptions.assert_called_once_with(timeout=45000)
        client.models.generate_content.assert_called_once_with(model="gemini-2.5-flash", contents="Pergunta")

    @mock.patch("responsegenerator.llm_client.Groq")
    def test_groq_client_flow_and_timeout(self, mocked_groq):
        llm = LLM.objects.create(
            usuario=self.user, nome="llama-3.3-70b-versatile", descricao="Groq", api_key="groq-key",
        )
        client = mocked_groq.return_value
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Resposta Groq"))]
        )

        result = call_configured_llm(llm, "Pergunta")

        self.assertEqual(result, "Resposta Groq")
        mocked_groq.assert_called_once_with(api_key="groq-key", timeout=45.0, max_retries=0)
        client.chat.completions.create.assert_called_once()

    @mock.patch("responsegenerator.llm_client.genai_types")
    @mock.patch("responsegenerator.llm_client.genai")
    def test_gemini_quota_error_is_not_silent(self, mocked_genai, mocked_types):
        llm = LLM.objects.create(
            usuario=self.user, nome="gemini-2.5-flash", descricao="Gemini", api_key="key",
        )
        mocked_genai.Client.return_value.models.generate_content.side_effect = RuntimeError(
            "429 RESOURCE_EXHAUSTED: quota exceeded"
        )

        with self.assertRaises(LLMServiceError) as raised:
            call_configured_llm(llm, "Pergunta")

        self.assertEqual(raised.exception.code, "quota_exceeded")
        self.assertIn("cota", str(raised.exception).lower())


class LLMConfigurationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="llm-owner", password="senha-segura")
        self.client.force_login(self.user)
        self.llm = LLM.objects.create(
            usuario=self.user,
            nome="gemini-2.5-flash",
            descricao="Gemini",
            api_key="old-secret-key-1234",
        )

    def test_edit_endpoint_persists_replaced_api_key(self):
        new_key = "new-secret-groq-key-9876"

        with self.assertLogs("responsegenerator.views", level="INFO") as captured:
            response = self.client.put(
                reverse("edit_llm_api", args=[self.llm.id]),
                data=json.dumps({
                    "nome": "llama-3.3-70b-versatile",
                    "descricao": "Groq",
                    "api_key": new_key,
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["key_updated"])
        self.assertEqual(payload["api_key_hint"], "••••9876")
        self.assertNotIn(new_key, response.content.decode())
        self.llm.refresh_from_db()
        self.assertEqual(self.llm.api_key, new_key)
        self.assertEqual(self.llm.nome, "llama-3.3-70b-versatile")
        self.assertEqual(self.llm.descricao, "Groq")
        self.assertIn("key_updated=True", " ".join(captured.output))

    def test_blank_api_key_preserves_current_persisted_key(self):
        response = self.client.put(
            reverse("edit_llm_api", args=[self.llm.id]),
            data=json.dumps({
                "nome": "gemini-2.5-pro",
                "descricao": "Gemini",
                "api_key": "",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["key_updated"])
        self.llm.refresh_from_db()
        self.assertEqual(self.llm.api_key, "old-secret-key-1234")
        self.assertEqual(self.llm.nome, "gemini-2.5-pro")

    def test_setup_page_exposes_only_masked_key_hint(self):
        response = self.client.get(reverse("setup_llm"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "••••1234")
        self.assertNotContains(response, "old-secret-key-1234")

    def test_edit_cannot_modify_another_users_llm(self):
        other = User.objects.create_user(username="other-owner", password="senha-segura")
        foreign_llm = LLM.objects.create(
            usuario=other,
            nome="modelo",
            descricao="Groq",
            api_key="foreign-key",
        )

        response = self.client.put(
            reverse("edit_llm_api", args=[foreign_llm.id]),
            data=json.dumps({
                "nome": "alterado",
                "descricao": "Gemini",
                "api_key": "stolen-replacement",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        foreign_llm.refresh_from_db()
        self.assertEqual(foreign_llm.api_key, "foreign-key")

    def test_new_configuration_persists_api_key(self):
        response = self.client.post(reverse("setup_llm"), data={
            "provider": "Groq",
            "model": "llama-3.1-8b-instant",
            "apiKey": "new-configuration-key",
        })

        self.assertRedirects(response, reverse("setup_llm"))
        created = LLM.objects.get(nome="llama-3.1-8b-instant")
        self.assertEqual(created.api_key, "new-configuration-key")
        self.assertEqual(created.usuario, self.user)


class AdminPublicMetricTests(TestCase):
    def setUp(self):
        self.admin = AdminPonderSec(nome="Admin", email="admin@example.com", ativo=True)
        self.admin.set_senha("senha-segura")
        self.admin.save()
        session = self.client.session
        session[ADMIN_SESSION_KEY] = self.admin.id
        session.save()

    def test_public_metrics_are_fixed_and_cannot_be_created_or_deleted(self):
        response = self.client.post(
            reverse("admin_pondersec_metricas_publicas"),
            data={"nome": "Fidelidade", "pontuacao_maxima": "9"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Metrica.objects.filter(usuario__isnull=True, nome="Fidelidade").exists())
        metrics = ensure_judge_metrics(None)
        self.assertEqual([item.nome for item in metrics], list(JUDGE_METRIC_NAMES))
        self.assertTrue(all(item.pontuacao_maxima == 5 and item.ativa for item in metrics))

        delete_response = self.client.delete(
            reverse("admin_pondersec_metrica_publica_deletar", args=[metrics[0].id])
        )
        self.assertEqual(delete_response.status_code, 409)
        self.assertTrue(Metrica.objects.filter(pk=metrics[0].id).exists())

    def test_public_metric_and_evaluation_pages_render_only_four_metrics(self):
        metric_response = self.client.get(reverse("admin_pondersec_metricas_publicas"))
        evaluation_response = self.client.get(reverse("admin_pondersec_avaliacoes_publicas"))
        self.assertEqual(metric_response.status_code, 200)
        self.assertEqual(evaluation_response.status_code, 200)
        for name in JUDGE_METRIC_NAMES:
            self.assertContains(metric_response, name)
        self.assertNotContains(metric_response, "Adicionar Métrica")


class PublicFormEvaluationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="form-owner", password="senha-segura")
        self.metrics = ensure_judge_metrics(self.owner)
        self.question = Questao.objects.create(usuario=self.owner, conteudo="Como evitar phishing?")
        self.answer = Resposta.objects.create(
            questao=self.question,
            conteudo_resposta="Verifique o remetente e não abra links suspeitos.",
        )
        self.form = Formulario.objects.create(nome="Avaliação de segurança", usuario=self.owner)
        self.form.questoes.add(self.question)
        self.url = reverse("responder_avaliacao_publica", args=[self.form.id])
        self.identity = {
            "nome": "Especialista",
            "email": "especialista@example.com",
            "profissao": "Analista de segurança",
        }

    def _scores_for(self, answer, value="4"):
        return {
            f"quanti_{answer.id}_{metric.id}": value
            for metric in self.metrics
        }

    def test_scores_start_empty_on_fixed_one_to_five_scale(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-max="5"', count=4)
        self.assertContains(response, 'data-val=""')
        for metric in self.metrics:
            self.assertContains(response, f'name="quanti_{self.answer.id}_{metric.id}"')

    def test_all_answers_and_all_four_metrics_must_be_scored(self):
        second_answer = Resposta.objects.create(questao=self.question, conteudo_resposta="Use MFA.")
        response = self.client.post(self.url, data={**self.identity, **self._scores_for(self.answer)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AvaliacaoFormulario.objects.count(), 0)
        self.assertFalse(AvaliacaoFormulario.objects.filter(resposta=second_answer).exists())

    def test_score_outside_range_is_rejected(self):
        response = self.client.post(self.url, data={**self.identity, **self._scores_for(self.answer, "6")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AvaliacaoFormulario.objects.count(), 0)

    def test_complete_four_metric_evaluation_is_saved(self):
        data = {**self.identity, **self._scores_for(self.answer)}
        for metric in self.metrics:
            data[f"quali_{self.answer.id}_{metric.id}"] = f"Justificativa de {metric.nome}."
        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "avaliacao/avaliacao_sucesso.html")
        self.assertEqual(AvaliacaoFormulario.objects.count(), 4)
        self.assertEqual(
            set(AvaliacaoFormulario.objects.values_list("metrica__nome", flat=True)),
            set(JUDGE_METRIC_NAMES),
        )
