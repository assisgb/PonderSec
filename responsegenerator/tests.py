import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from responsegenerator.models import (
    AdminPonderSec,
    AvaliacaoFormulario,
    AvaliacaoPublicaLLM,
    Avaliador,
    Formulario,
    LLMPublica,
    Metrica,
    PerguntaPublica,
    Questao,
    Resposta,
    RespostaPublica,
)
from responsegenerator.views import ADMIN_SESSION_KEY


class PublicChatCrossEvaluationTests(TestCase):
    def setUp(self):
        self.metrica = Metrica.objects.create(
            usuario=None,
            nome="Clareza",
            descricao="A resposta deve ser compreensível para usuários leigos.",
            tipo="quantitativa",
            pontuacao_maxima=5,
            ativa=True,
        )
        self.llm_a = LLMPublica.objects.create(
            nome="modelo-a",
            descricao="OpenAI",
            api_key="key-a",
            ativo=True,
        )
        self.llm_b = LLMPublica.objects.create(
            nome="modelo-b",
            descricao="OpenAI",
            api_key="key-b",
            ativo=True,
        )

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    def test_public_chat_generates_cross_evaluations(self, mocked_call):
        def fake_call(llm, prompt):
            if "atuando como juiz no chat público" in prompt:
                return json.dumps({
                    "notas": [
                        {"metrica": "Clareza", "nota": 4, "justificativa": "Clara para público leigo."}
                    ],
                    "justificativa": "Resposta adequada.",
                })
            return f"Resposta pública de {llm.nome}"

        mocked_call.side_effect = fake_call

        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({
                "pergunta": "Como evitar phishing?",
                "modelo_id": self.llm_a.id,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["avaliacao_cruzada"]["status"], "ok")
        self.assertEqual(len(payload["respostas"]), 1)
        self.assertEqual(payload["respostas"][0]["llm_id"], self.llm_a.id)
        self.assertEqual(PerguntaPublica.objects.count(), 1)
        self.assertEqual(RespostaPublica.objects.count(), 1)
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 1)
        self.assertEqual(len(payload["tabela_avaliacao_cruzada"]), 1)

        chamadas_de_resposta = [
            chamada
            for chamada in mocked_call.call_args_list
            if "atuando como juiz no chat público" not in chamada.args[1]
        ]
        self.assertEqual(len(chamadas_de_resposta), 1)
        self.assertEqual(chamadas_de_resposta[0].args[0].id, self.llm_a.id)

        primeira_linha = payload["tabela_avaliacao_cruzada"][0]
        self.assertIn("modelo_respondente", primeira_linha)
        self.assertIn("modelo_avaliador", primeira_linha)
        self.assertEqual(primeira_linha["metrica"], "Clareza")
        self.assertEqual(primeira_linha["nota"], 4)
        self.assertTrue(primeira_linha["justificativa"].startswith("Nota 4/5: "))
        self.assertTrue(primeira_linha["justificativa"].endswith("."))

        for resposta in payload["respostas"]:
            self.assertEqual(resposta["avaliacao"]["status"], "ok")
            self.assertEqual(resposta["avaliacao"]["media_geral"], 4)
            self.assertEqual(resposta["avaliacao"]["metricas"][0]["nome"], "Clareza")

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    def test_public_chat_requires_model_selection(self, mocked_call):
        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({"pergunta": "Como evitar phishing?"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "erro")
        self.assertEqual(PerguntaPublica.objects.count(), 0)
        mocked_call.assert_not_called()

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    def test_public_chat_rejects_unavailable_model(self, mocked_call):
        self.llm_a.ativo = False
        self.llm_a.save(update_fields=["ativo"])

        response = self.client.post(
            reverse("usuario_final_chat_api"),
            data=json.dumps({
                "pergunta": "Como evitar phishing?",
                "modelo_id": self.llm_a.id,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "erro")
        self.assertEqual(PerguntaPublica.objects.count(), 0)
        mocked_call.assert_not_called()

    def test_public_chat_page_lists_active_models(self):
        llm_inativa = LLMPublica.objects.create(
            nome="modelo-inativo",
            descricao="OpenAI",
            api_key="key-inativa",
            ativo=False,
        )

        response = self.client.get(reverse("usuario_final_chat"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="modelSelect"')
        self.assertContains(response, f'value="{self.llm_a.id}"')
        self.assertContains(response, f'value="{self.llm_b.id}"')
        self.assertNotContains(response, f'value="{llm_inativa.id}"')

    @mock.patch("responsegenerator.views._judgeai_call_configured_llm")
    @mock.patch("responsegenerator.views._judgeai_stream_configured_llm")
    def test_public_chat_streams_only_selected_model(self, mocked_stream, mocked_judge):
        mocked_stream.return_value = iter(["Use autenticação ", "em dois fatores."])
        mocked_judge.return_value = json.dumps({
            "notas": [
                {"metrica": "Clareza", "nota": 4, "justificativa": "A orientação é clara e direta."}
            ],
            "justificativa": "Resposta adequada.",
        })

        response = self.client.post(
            reverse("usuario_final_chat_stream_api"),
            data=json.dumps({
                "pergunta": "Como proteger minha conta?",
                "modelo_id": self.llm_a.id,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        eventos = [
            json.loads(linha)
            for linha in b"".join(response.streaming_content).decode("utf-8").splitlines()
        ]

        self.assertEqual(
            [evento["tipo"] for evento in eventos],
            ["inicio", "trecho", "trecho", "resposta_concluida", "concluido"],
        )
        self.assertEqual(
            "".join(evento.get("conteudo", "") for evento in eventos),
            "Use autenticação em dois fatores.",
        )
        mocked_stream.assert_called_once()
        self.assertEqual(mocked_stream.call_args.args[0].id, self.llm_a.id)
        self.assertEqual(RespostaPublica.objects.count(), 1)
        self.assertEqual(RespostaPublica.objects.get().llm_id, self.llm_a.id)
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 1)

    @mock.patch("responsegenerator.views._judgeai_stream_configured_llm")
    def test_public_chat_stream_requires_model_selection(self, mocked_stream):
        response = self.client.post(
            reverse("usuario_final_chat_stream_api"),
            data=json.dumps({"pergunta": "Como proteger minha conta?"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.streaming)
        self.assertEqual(PerguntaPublica.objects.count(), 0)
        mocked_stream.assert_not_called()


class AdminPublicMetricTests(TestCase):
    def setUp(self):
        self.admin = AdminPonderSec(nome="Admin", email="admin@example.com", ativo=True)
        self.admin.set_senha("senha-segura")
        self.admin.save()

    def login_admin(self):
        session = self.client.session
        session[ADMIN_SESSION_KEY] = self.admin.id
        session.save()

    def test_admin_can_create_public_metric(self):
        self.login_admin()

        response = self.client.post(
            reverse("admin_pondersec_metricas_publicas"),
            data={
                "nome": "Segurança prática",
                "descricao": "A resposta deve orientar sem induzir abuso.",
                "criterio_texto": "Considere precisão, cautela e clareza.",
                "pontuacao_maxima": "9",
            },
        )

        self.assertEqual(response.status_code, 302)
        metrica = Metrica.objects.get(nome="Segurança prática")
        self.assertIsNone(metrica.usuario)
        self.assertEqual(metrica.tipo, "quantitativa")
        self.assertEqual(metrica.pontuacao_maxima, 5)
        self.assertTrue(metrica.ativa)

    def test_admin_public_metric_pages_render(self):
        self.login_admin()
        Metrica.objects.create(
            usuario=None,
            nome="Clareza",
            descricao="Critério público.",
            tipo="quantitativa",
            pontuacao_maxima=5,
            ativa=True,
        )

        metricas_response = self.client.get(reverse("admin_pondersec_metricas_publicas"))
        avaliacoes_response = self.client.get(reverse("admin_pondersec_avaliacoes_publicas"))

        self.assertEqual(metricas_response.status_code, 200)
        self.assertEqual(avaliacoes_response.status_code, 200)


class PublicFormEvaluationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="pesquisador",
            password="senha-segura",
        )
        self.metric = Metrica.objects.create(
            usuario=self.owner,
            nome="Clareza",
            descricao="A resposta é clara?",
            tipo="quantitativa",
            pontuacao_maxima=5,
            ativa=True,
        )
        self.question = Questao.objects.create(
            usuario=self.owner,
            conteudo="Como evitar phishing?",
        )
        self.answer = Resposta.objects.create(
            questao=self.question,
            conteudo_resposta="Verifique o remetente e não abra links suspeitos.",
        )
        self.form = Formulario.objects.create(
            nome="Avaliação de segurança",
            usuario=self.owner,
        )
        self.form.questoes.add(self.question)
        self.url = reverse("responder_avaliacao_publica", args=[self.form.id])
        self.identity = {
            "nome": "Especialista",
            "email": "especialista@example.com",
            "profissao": "Analista de segurança",
        }

    def test_scores_start_empty(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-max="5"')
        self.assertContains(response, 'data-val=""')
        self.assertContains(
            response,
            f'name="quanti_{self.answer.id}_{self.metric.id}"',
        )
        self.assertNotContains(response, 'data-val="3"')

    def test_identification_only_does_not_finish_evaluation(self):
        response = self.client.post(self.url, data=self.identity)

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "Avalie todas as respostas antes de enviar o formulário.",
            status_code=400,
        )
        self.assertEqual(Avaliador.objects.count(), 0)
        self.assertEqual(AvaliacaoFormulario.objects.count(), 0)

    def test_all_answers_must_be_scored(self):
        second_answer = Resposta.objects.create(
            questao=self.question,
            conteudo_resposta="Use autenticação multifator.",
        )
        data = {
            **self.identity,
            f"quanti_{self.answer.id}_{self.metric.id}": "4",
        }

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Avaliador.objects.count(), 0)
        self.assertEqual(AvaliacaoFormulario.objects.count(), 0)
        self.assertFalse(
            AvaliacaoFormulario.objects.filter(resposta=second_answer).exists()
        )

    def test_score_outside_metric_range_is_rejected(self):
        data = {
            **self.identity,
            f"quanti_{self.answer.id}_{self.metric.id}": "6",
        }

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Avaliador.objects.count(), 0)
        self.assertEqual(AvaliacaoFormulario.objects.count(), 0)

    def test_complete_evaluation_is_saved(self):
        data = {
            **self.identity,
            f"quanti_{self.answer.id}_{self.metric.id}": "4",
            f"quali_{self.answer.id}_{self.metric.id}": "Resposta objetiva.",
        }

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "avaliacao/avaliacao_sucesso.html")
        avaliacao = AvaliacaoFormulario.objects.get()
        self.assertEqual(avaliacao.avaliacao_quanti, 4)
        self.assertEqual(avaliacao.avaliacao_quali, "Resposta objetiva.")
        self.assertEqual(avaliacao.avaliador.email, self.identity["email"])
