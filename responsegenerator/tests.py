import gzip
import json
from importlib import import_module
from types import SimpleNamespace
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from responsegenerator.judgeai_metrics import JUDGE_METRIC_NAMES, ensure_judge_metrics
from responsegenerator.llm_client import (
    LLMServiceError,
    call_configured_llm,
    stream_configured_llm,
)
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

    def test_public_page_externalizes_theme_css_and_is_gzip_compressed(self):
        response = self.client.get(
            reverse("usuario_final_chat"),
            HTTP_ACCEPT_ENCODING="gzip",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Encoding"], "gzip")
        html = gzip.decompress(response.content).decode("utf-8")
        self.assertIn("/static/language_switcher.css", html)
        self.assertNotIn("TEMA CLARO — [data-theme=", html)

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
        concluido = events[-1]
        self.assertEqual(concluido["avaliacao"]["status"], "pendente")
        self.assertTrue(concluido["avaliacao_token"])
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 0)
        mocked_judge.assert_not_called()

        evaluation_response = self.client.post(
            reverse("usuario_final_chat_avaliacao_api"),
            data=json.dumps({
                "resposta_id": concluido["resposta_id"],
                "avaliacao_token": concluido["avaliacao_token"],
            }),
            content_type="application/json",
        )

        self.assertEqual(evaluation_response.status_code, 200)
        evaluation_payload = evaluation_response.json()
        self.assertEqual(evaluation_payload["status"], "ok")
        self.assertEqual(evaluation_payload["avaliacao_cruzada"]["status"], "ok")
        self.assertEqual(len(evaluation_payload["tabela_avaliacao_cruzada"]), 4)
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 4)
        self.assertEqual(AvaliacaoPublicaLLM.objects.values("juiz_id").distinct().get()["juiz_id"], self.llm_b.id)

        # Repetir a requisição assinada deve apenas devolver o resultado persistido,
        # sem consumir outra chamada do provedor.
        repeated_response = self.client.post(
            reverse("usuario_final_chat_avaliacao_api"),
            data=json.dumps({
                "resposta_id": concluido["resposta_id"],
                "avaliacao_token": concluido["avaliacao_token"],
            }),
            content_type="application/json",
        )
        self.assertEqual(repeated_response.status_code, 200)
        self.assertEqual(repeated_response.json()["avaliacao_cruzada"]["mensagem"], "Avaliação cruzada já concluída.")
        self.assertEqual(mocked_judge.call_count, 1)

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    @mock.patch("responsegenerator.views._judgeai_stream_configured_llm")
    def test_public_chat_evaluation_rejects_tampered_or_mismatched_token(self, mocked_stream, mocked_judge):
        mocked_stream.return_value = iter(["Resposta segura."])
        response = self.client.post(
            reverse("usuario_final_chat_stream_api"),
            data=json.dumps({"pergunta": "Como proteger minha conta?", "modelo_id": self.llm_a.id}),
            content_type="application/json",
        )
        events = [
            json.loads(line)
            for line in b"".join(response.streaming_content).decode().splitlines()
        ]
        concluido = events[-1]

        tampered_response = self.client.post(
            reverse("usuario_final_chat_avaliacao_api"),
            data=json.dumps({
                "resposta_id": concluido["resposta_id"],
                "avaliacao_token": concluido["avaliacao_token"] + "adulterado",
            }),
            content_type="application/json",
        )
        self.assertEqual(tampered_response.status_code, 403)

        mismatched_response = self.client.post(
            reverse("usuario_final_chat_avaliacao_api"),
            data=json.dumps({
                "resposta_id": concluido["resposta_id"] + 1,
                "avaliacao_token": concluido["avaliacao_token"],
            }),
            content_type="application/json",
        )
        self.assertEqual(mismatched_response.status_code, 403)
        mocked_judge.assert_not_called()
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 0)

    def test_public_chat_validates_model_selection(self):
        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({"pergunta": "Como evitar phishing?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PerguntaPublica.objects.count(), 0)

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm", return_value="Resposta rápida")
    def test_non_stream_api_can_defer_judgeai_without_hiding_the_answer(self, mocked_call):
        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({
                "pergunta": "Como ativar MFA?",
                "modelo_id": self.llm_a.id,
                "avaliar": False,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["avaliacao_cruzada"]["status"], "pendente")
        self.assertTrue(payload["avaliacao_token"])
        self.assertEqual(mocked_call.call_count, 1)
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 0)

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    def test_evaluation_claim_prevents_a_second_concurrent_judge_call(self, mocked_judge):
        question = PerguntaPublica.objects.create(conteudo="Como usar MFA?")
        answer = RespostaPublica.objects.create(
            pergunta=question,
            llm=self.llm_a,
            conteudo_resposta="Ative MFA.",
            avaliacao_estado=RespostaPublica.AVALIACAO_PROCESSANDO,
            avaliacao_iniciada_em=timezone.now(),
        )
        token = signing.dumps(
            {"resposta_id": answer.id},
            salt="responsegenerator.public-evaluation",
            compress=True,
        )

        response = self.client.post(
            reverse("usuario_final_chat_avaliacao_api"),
            data=json.dumps({"resposta_id": answer.id, "avaliacao_token": token}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "processando")
        mocked_judge.assert_not_called()

    @override_settings(PUBLIC_CHAT_RATE_LIMIT=1)
    def test_public_chat_rate_limit_rejects_bursts_before_provider_call(self):
        cache.clear()
        payload = json.dumps({"pergunta": "Pergunta", "modelo_id": self.llm_a.id})
        try:
            first = self.client.post(
                reverse("usuario_final_chat_stream_api"),
                data=payload,
                content_type="application/json",
            )
            second = self.client.post(
                reverse("usuario_final_chat_stream_api"),
                data=payload,
                content_type="application/json",
            )
        finally:
            cache.clear()

        self.assertNotEqual(first.status_code, 429)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second["Retry-After"], "60")


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
            self.assertIn(result["juiz_id"], {llm_a.id, llm_b.id})
            self.assertEqual([item["metrica"] for item in result["notas"]], list(JUDGE_METRIC_NAMES))
        self.assertEqual(mocked_call.call_count, 2)

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm", return_value=judge_payload())
    def test_repeated_judge_run_does_not_inflate_online_judge_counters(self, mocked_call):
        llm_a = LLM.objects.create(usuario=self.user, nome="model-a", descricao="Groq", api_key="a")
        llm_b = LLM.objects.create(usuario=self.user, nome="model-b", descricao="Gemini", api_key="b")
        question = Questao.objects.create(usuario=self.user, conteudo="Como evitar phishing?")
        Resposta.objects.create(questao=question, llm=llm_a, conteudo_resposta="Resposta A")
        Resposta.objects.create(questao=question, llm=llm_b, conteudo_resposta="Resposta B")
        request_data = json.dumps({
            "questao_ids": [question.id],
            "juiz_ids": [llm_a.id, llm_b.id],
        })

        first = self.client.post(
            reverse("juizes_executar_avaliacao"),
            data=request_data,
            content_type="application/json",
        )
        second = self.client.post(
            reverse("juizes_executar_avaliacao"),
            data=request_data,
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(AvaliacaoJuiz.objects.count(), 8)
        dashboard_response = self.client.get(reverse("dashboard_avaliacoes"))
        dashboard = json.loads(dashboard_response.context["dashboard_json"])
        self.assertEqual(dashboard["resumo"]["notas_juizes"], 8)
        self.assertEqual(dashboard["resumo"]["juizes_online"], 2)
        self.assertEqual(mocked_call.call_count, 4)

    def test_judge_comparator_merges_new_pairs_and_replaces_repeated_pairs(self):
        response = self.client.get(reverse("juizes_comparador"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "function mergeEvaluationResults(currentResults, newResults)")
        self.assertContains(response, "newResults.forEach(item => merged.set(resultIdentity(item), item));")
        self.assertContains(response, "state.results = mergeEvaluationResults(state.results, newResults);")
        self.assertNotContains(response, "const toAdd = newResults.filter")

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

    def test_database_prevents_duplicate_metric_names_for_user_and_public_scope(self):
        metric = self.metrics[0]
        with self.assertRaises(IntegrityError), transaction.atomic():
            Metrica.objects.create(
                usuario=self.user,
                nome=metric.nome,
                descricao="Duplicada",
                tipo="quantitativa",
            )

        public_metric = ensure_judge_metrics(None)[0]
        with self.assertRaises(IntegrityError), transaction.atomic():
            Metrica.objects.create(
                usuario=None,
                nome=public_metric.nome,
                descricao="Duplicada pública",
                tipo="quantitativa",
            )


class ResearchBatchGenerationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="batch-user",
            password="senha-segura",
        )
        self.client.force_login(self.user)
        self.groq = LLM.objects.create(
            usuario=self.user,
            nome="llama-3.3-70b-versatile",
            descricao="Groq",
            api_key="groq-key",
        )
        self.gemini = LLM.objects.create(
            usuario=self.user,
            nome="gemini-2.5-flash",
            descricao="Gemini",
            api_key="gemini-key",
        )
        self.question = Questao.objects.create(
            usuario=self.user,
            conteudo="Como reduzir ataques de phishing?",
        )
        self.endpoint = reverse(
            "gerar_respostas_ia_faltantes",
            args=[self.question.id],
        )

    @mock.patch("responsegenerator.views._call_llm_in_worker")
    def test_repeated_batch_call_is_idempotent(self, mocked_call):
        mocked_call.side_effect = lambda llm, _prompt: f"Resposta de {llm.nome}"

        first = self.client.post(self.endpoint)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "ok")
        self.assertEqual(first.json()["respondidas"], 2)
        self.assertEqual(Resposta.objects.filter(questao=self.question).count(), 2)
        self.assertEqual(mocked_call.call_count, 2)

        mocked_call.reset_mock()
        second = self.client.post(self.endpoint)

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "ja_completo")
        self.assertEqual(Resposta.objects.filter(questao=self.question).count(), 2)
        mocked_call.assert_not_called()

    @mock.patch("responsegenerator.views._call_llm_in_worker")
    def test_partial_result_is_saved_and_retry_calls_only_missing_model(self, mocked_call):
        def first_attempt(llm, _prompt):
            if llm.id == self.gemini.id:
                raise LLMServiceError("Falha temporária do Gemini", code="timeout")
            return "Resposta Groq preservada"

        mocked_call.side_effect = first_attempt
        first = self.client.post(self.endpoint)

        self.assertEqual(first.status_code, 207)
        self.assertEqual(first.json()["status"], "parcial")
        self.assertEqual(first.json()["respondidas"], 1)
        saved = Resposta.objects.get(questao=self.question)
        self.assertEqual(saved.llm, self.groq)
        self.assertEqual(saved.conteudo_resposta, "Resposta Groq preservada")

        consultation = self.client.get(reverse("executar_consulta"))
        rendered_question = next(
            item
            for item in consultation.context["questoes"]
            if item.id == self.question.id
        )
        self.assertEqual(rendered_question.status_consulta, "parcial")
        self.assertTrue(rendered_question.pode_executar)
        self.assertContains(consultation, "Parcial")

        mocked_call.reset_mock()
        mocked_call.side_effect = lambda _llm, _prompt: "Resposta Gemini recuperada"
        second = self.client.post(self.endpoint)

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "ok")
        self.assertEqual(second.json()["respondidas"], 2)
        self.assertEqual(mocked_call.call_count, 1)
        self.assertEqual(mocked_call.call_args.args[0].id, self.gemini.id)
        self.assertEqual(Resposta.objects.filter(questao=self.question).count(), 2)

    def test_human_answer_does_not_mark_ai_generation_complete(self):
        Resposta.objects.create(
            questao=self.question,
            llm=None,
            conteudo_resposta="Resposta humana",
        )

        response = self.client.get(reverse("executar_consulta"))
        rendered_question = next(
            item
            for item in response.context["questoes"]
            if item.id == self.question.id
        )

        self.assertEqual(rendered_question.respostas_ativas_total, 0)
        self.assertEqual(rendered_question.status_consulta, "pendente")
        self.assertTrue(rendered_question.pode_executar)

    def test_consultation_renders_select_all_for_executable_questions(self):
        response = self.client.get(reverse("executar_consulta"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="btnSelectAll"')
        self.assertContains(response, 'aria-pressed="false"')
        self.assertContains(response, "Selecionar todas")
        self.assertContains(response, "toggleVisibleQuestions()")

    def test_database_rejects_duplicate_ai_answer_but_allows_human_answers(self):
        Resposta.objects.create(
            questao=self.question,
            llm=self.groq,
            conteudo_resposta="Primeira resposta",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Resposta.objects.create(
                questao=self.question,
                llm=self.groq,
                conteudo_resposta="Resposta duplicada",
            )

        Resposta.objects.create(
            questao=self.question,
            llm=None,
            conteudo_resposta="Resposta humana 1",
        )
        Resposta.objects.create(
            questao=self.question,
            llm=None,
            conteudo_resposta="Resposta humana 2",
        )
        self.assertEqual(
            Resposta.objects.filter(questao=self.question, llm__isnull=True).count(),
            2,
        )

    def test_removing_llm_preserves_its_existing_answers(self):
        answer = Resposta.objects.create(
            questao=self.question,
            llm=self.groq,
            conteudo_resposta="Resposta histórica",
        )

        response = self.client.delete(reverse("delete_llm", args=[self.groq.id]))

        self.assertEqual(response.status_code, 200)
        self.groq.refresh_from_db()
        self.assertFalse(self.groq.ativo)
        self.assertTrue(Resposta.objects.filter(pk=answer.id).exists())


class QuestionUploadOptimizationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="upload-user", password="senha")
        self.client.force_login(self.user)

    def test_json_upload_creates_questions_and_categories_in_one_atomic_batch(self):
        content = json.dumps([
            {"pergunta": "Pergunta 1", "categoria": "Web", "resposta": "Resposta 1"},
            {"pergunta": "Pergunta 2", "categoria": "Web"},
            {"pergunta": "Pergunta 3", "categoria": "Rede"},
        ]).encode("utf-8")
        upload = SimpleUploadedFile("perguntas.json", content, content_type="application/json")

        response = self.client.post(
            reverse("upload_perguntas"),
            data={"arquivo_upload": upload},
        )

        self.assertRedirects(response, reverse("questoes"))
        self.assertEqual(Questao.objects.filter(usuario=self.user).count(), 3)
        self.assertEqual(
            set(Questao.objects.values_list("categoria__nome_categoria", flat=True)),
            {"Web", "Rede"},
        )
        self.assertEqual(
            Questao.objects.get(conteudo="Pergunta 1").resposta_humana,
            "Resposta 1",
        )

    def test_invalid_json_does_not_partially_persist_valid_prefix(self):
        content = json.dumps([
            {"pergunta": "Não deve ser salva", "categoria": "Temporária"},
            "item inválido",
        ]).encode("utf-8")
        upload = SimpleUploadedFile("perguntas.json", content, content_type="application/json")

        response = self.client.post(
            reverse("upload_perguntas"),
            data={"arquivo_upload": upload},
        )

        self.assertRedirects(response, reverse("questoes"))
        self.assertFalse(Questao.objects.filter(usuario=self.user).exists())


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

    @mock.patch("responsegenerator.llm_client.genai_types")
    @mock.patch("responsegenerator.llm_client.genai")
    def test_gemini_authorization_key_uses_interactions_api(self, mocked_genai, mocked_types):
        llm = LLM.objects.create(
            usuario=self.user,
            nome="gemini-3.5-flash",
            descricao="Gemini",
            api_key="AQ." + ("a" * 50),
        )
        client = mocked_genai.Client.return_value
        client.interactions.create.return_value = SimpleNamespace(output_text="Resposta Gemini AQ")

        result = call_configured_llm(llm, "Pergunta")

        self.assertEqual(result, "Resposta Gemini AQ")
        client.interactions.create.assert_called_once_with(
            model="gemini-3.5-flash",
            input="Pergunta",
        )
        client.models.generate_content.assert_not_called()

    @mock.patch("responsegenerator.llm_client.genai_types")
    @mock.patch("responsegenerator.llm_client.genai")
    def test_gemini_authorization_key_streams_interaction_text(self, mocked_genai, mocked_types):
        llm = LLM.objects.create(
            usuario=self.user,
            nome="gemini-3.5-flash",
            descricao="Gemini",
            api_key="AQ." + ("b" * 50),
        )
        client = mocked_genai.Client.return_value
        client.interactions.create.return_value = iter([
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="text", text="Resposta "),
            ),
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="text", text="Gemini AQ"),
            ),
        ])

        result = "".join(stream_configured_llm(llm, "Pergunta"))

        self.assertEqual(result, "Resposta Gemini AQ")
        client.interactions.create.assert_called_once_with(
            model="gemini-3.5-flash",
            input="Pergunta",
            stream=True,
        )
        client.models.generate_content_stream.assert_not_called()

    @mock.patch("responsegenerator.llm_client.genai_types")
    @mock.patch("responsegenerator.llm_client.genai")
    def test_gemini_rejected_authorization_key_has_specific_error(self, mocked_genai, mocked_types):
        llm = LLM.objects.create(
            usuario=self.user,
            nome="gemini-3.5-flash",
            descricao="Gemini",
            api_key="AQ." + ("c" * 50),
        )
        mocked_genai.Client.return_value.interactions.create.side_effect = RuntimeError(
            "401 UNAUTHENTICATED: ACCESS_TOKEN_TYPE_UNSUPPORTED"
        )

        with self.assertRaises(LLMServiceError) as raised:
            call_configured_llm(llm, "Pergunta")

        self.assertEqual(raised.exception.code, "gemini_auth_key_rejected")
        self.assertIn("aq.", str(raised.exception).lower())

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

    @mock.patch("responsegenerator.llm_client.openai")
    def test_deepseek_uses_official_api_endpoint(self, mocked_openai):
        llm = LLM.objects.create(
            usuario=self.user,
            nome="deepseek-v4-flash",
            descricao="DeepSeek",
            api_key="deepseek-key",
        )
        client = mocked_openai.OpenAI.return_value
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Resposta DeepSeek"))]
        )

        result = call_configured_llm(llm, "Pergunta")

        self.assertEqual(result, "Resposta DeepSeek")
        mocked_openai.OpenAI.assert_called_once_with(
            api_key="deepseek-key",
            timeout=45.0,
            max_retries=0,
            base_url="https://api.deepseek.com",
        )

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
        self.assertContains(response, 'class="likert-tick" data-pos="', count=20)
        for metric in self.metrics:
            self.assertContains(response, f'name="quanti_{self.answer.id}_{metric.id}"')

    def test_public_form_has_responsive_layout_and_touch_navigation(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "viewport-fit=cover")
        self.assertContains(response, "touch-action: pan-y")
        self.assertContains(response, "max-width: 1480px")
        self.assertContains(response, "grid-template-columns: repeat(2, minmax(0, 1fr))")
        self.assertContains(response, "max-height: clamp(160px, 32dvh, 360px)")
        self.assertContains(response, "carouselShell.addEventListener('touchstart'")

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

    def test_resubmission_replaces_scores_without_inflating_counters(self):
        first = self.client.post(
            self.url,
            data={**self.identity, **self._scores_for(self.answer, "2")},
        )
        second = self.client.post(
            self.url,
            data={**self.identity, **self._scores_for(self.answer, "5")},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Avaliador.objects.count(), 1)
        self.assertEqual(AvaliacaoFormulario.objects.count(), 4)
        self.assertEqual(
            set(AvaliacaoFormulario.objects.values_list("avaliacao_quanti", flat=True)),
            {5},
        )

        self.client.force_login(self.owner)
        list_response = self.client.get(reverse("avaliacao"))
        form_from_context = next(
            form
            for form in list_response.context["formularios"]
            if form.id == self.form.id
        )
        self.assertEqual(form_from_context.avaliadores_total, 1)

        dashboard_response = self.client.get(reverse("dashboard_avaliacoes"))
        dashboard = json.loads(dashboard_response.context["dashboard_json"])
        self.assertEqual(dashboard["resumo"]["notas_especialistas"], 4)
        self.assertEqual(dashboard["resumo"]["avaliadores_humanos"], 1)

    def test_form_counter_ignores_evaluator_without_saved_scores(self):
        Avaliador.objects.create(
            formulario=self.form,
            **self.identity,
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("avaliacao"))
        form_from_context = next(
            form
            for form in response.context["formularios"]
            if form.id == self.form.id
        )

        self.assertEqual(form_from_context.avaliadores_total, 0)

    def test_dashboard_ignores_scores_from_questions_removed_from_form(self):
        removed_question = Questao.objects.create(
            usuario=self.owner,
            conteudo="Pergunta removida do formulário",
        )
        removed_answer = Resposta.objects.create(
            questao=removed_question,
            conteudo_resposta="Resposta histórica.",
        )
        current_evaluator = Avaliador.objects.create(
            formulario=self.form,
            **self.identity,
        )
        stale_evaluator = Avaliador.objects.create(
            formulario=self.form,
            nome="Avaliador histórico",
            email="historico@example.com",
            profissao="Analista",
        )
        AvaliacaoFormulario.objects.bulk_create([
            AvaliacaoFormulario(
                usuario=self.owner,
                avaliador=evaluator,
                resposta=answer,
                metrica=metric,
                avaliacao_quanti=value,
            )
            for evaluator, answer, value in (
                (current_evaluator, self.answer, 5),
                (current_evaluator, removed_answer, 1),
                (stale_evaluator, removed_answer, 1),
            )
            for metric in self.metrics
        ])
        self.client.force_login(self.owner)

        list_response = self.client.get(reverse("avaliacao"))
        form_from_context = next(
            form
            for form in list_response.context["formularios"]
            if form.id == self.form.id
        )
        dashboard_response = self.client.get(reverse("dashboard_avaliacoes"))
        dashboard = json.loads(dashboard_response.context["dashboard_json"])

        self.assertEqual(form_from_context.avaliadores_total, 1)
        self.assertEqual(dashboard["resumo"]["notas_especialistas"], 4)
        self.assertEqual(dashboard["resumo"]["avaliadores_humanos"], 1)

    def test_database_rejects_duplicate_score_for_same_evaluator(self):
        evaluator = Avaliador.objects.create(
            formulario=self.form,
            **self.identity,
        )
        AvaliacaoFormulario.objects.create(
            usuario=self.owner,
            avaliador=evaluator,
            resposta=self.answer,
            metrica=self.metrics[0],
            avaliacao_quanti=4,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AvaliacaoFormulario.objects.create(
                usuario=self.owner,
                avaliador=evaluator,
                resposta=self.answer,
                metrica=self.metrics[0],
                avaliacao_quanti=5,
            )

    def test_same_evaluator_is_counted_in_every_completed_form(self):
        second_question = Questao.objects.create(
            usuario=self.owner,
            conteudo="Como proteger uma conta?",
        )
        second_answer = Resposta.objects.create(
            questao=second_question,
            conteudo_resposta="Use uma senha exclusiva e autenticação em dois fatores.",
        )
        second_form = Formulario.objects.create(
            nome="Avaliação de autenticação",
            usuario=self.owner,
        )
        second_form.questoes.add(second_question)

        first_response = self.client.post(
            self.url,
            data={**self.identity, **self._scores_for(self.answer)},
        )
        second_response = self.client.post(
            reverse("responder_avaliacao_publica", args=[second_form.id]),
            data={**self.identity, **self._scores_for(second_answer)},
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(Avaliador.objects.count(), 2)
        self.assertEqual(self.form.avaliadores.count(), 1)
        self.assertEqual(second_form.avaliadores.count(), 1)
        self.assertEqual(AvaliacaoFormulario.objects.count(), 8)

        self.client.force_login(self.owner)
        list_response = self.client.get(reverse("avaliacao"))
        counts_by_form = {
            form.id: form.avaliadores_total
            for form in list_response.context["formularios"]
        }
        self.assertEqual(counts_by_form[self.form.id], 1)
        self.assertEqual(counts_by_form[second_form.id], 1)

        dashboard_response = self.client.get(reverse("dashboard_avaliacoes"))
        dashboard = json.loads(dashboard_response.context["dashboard_json"])
        self.assertEqual(dashboard["resumo"]["avaliadores_humanos"], 1)

    def test_migration_recovers_previous_form_counters(self):
        second_question = Questao.objects.create(
            usuario=self.owner,
            conteudo="Como configurar MFA?",
        )
        second_answer = Resposta.objects.create(
            questao=second_question,
            conteudo_resposta="Cadastre um aplicativo autenticador.",
        )
        second_form = Formulario.objects.create(
            nome="Avaliação de MFA",
            usuario=self.owner,
        )
        second_form.questoes.add(second_question)

        evaluator = Avaliador.objects.create(
            nome=self.identity["nome"],
            email=self.identity["email"],
            profissao=self.identity["profissao"],
            formulario=second_form,
        )
        AvaliacaoFormulario.objects.bulk_create([
            AvaliacaoFormulario(
                usuario=self.owner,
                avaliador=evaluator,
                resposta=answer,
                metrica=metric,
                avaliacao_quanti=4,
            )
            for answer in (self.answer, second_answer)
            for metric in self.metrics
        ])

        migration = import_module(
            "responsegenerator.migrations.0017_evaluator_per_form"
        )
        migration.restore_evaluator_form_links(
            django_apps,
            SimpleNamespace(connection=connection),
        )

        self.assertEqual(self.form.avaliadores.count(), 1)
        self.assertEqual(second_form.avaliadores.count(), 1)
        self.assertEqual(
            Avaliador.objects.filter(email=self.identity["email"]).count(),
            2,
        )

        repair_migration = import_module(
            "responsegenerator.migrations.0019_repair_evaluator_score_form_links"
        )
        repair_migration.repair_evaluator_score_form_links(
            django_apps,
            SimpleNamespace(connection=connection),
        )

        first_form_evaluator = Avaliador.objects.get(
            email=self.identity["email"],
            formulario=self.form,
        )
        second_form_evaluator = Avaliador.objects.get(
            email=self.identity["email"],
            formulario=second_form,
        )
        self.assertEqual(
            AvaliacaoFormulario.objects.filter(
                avaliador=first_form_evaluator,
                resposta=self.answer,
            ).count(),
            4,
        )
        self.assertEqual(
            AvaliacaoFormulario.objects.filter(
                avaliador=second_form_evaluator,
                resposta=second_answer,
            ).count(),
            4,
        )
