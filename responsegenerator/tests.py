import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from responsegenerator.models import (
    AdminPonderSec,
    AvaliacaoPublicaLLM,
    LLMPublica,
    Metrica,
    PerguntaPublica,
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
            if "Retorne somente JSON válido" in prompt:
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
            data=json.dumps({"pergunta": "Como evitar phishing?"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["avaliacao_cruzada"]["status"], "ok")
        self.assertEqual(len(payload["respostas"]), 2)
        self.assertEqual(PerguntaPublica.objects.count(), 1)
        self.assertEqual(RespostaPublica.objects.count(), 2)
        self.assertEqual(AvaliacaoPublicaLLM.objects.count(), 2)
        self.assertEqual(len(payload["tabela_avaliacao_cruzada"]), 2)

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
