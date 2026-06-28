import json
from collections import Counter
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from responsegenerator import urls as responsegenerator_urls
from responsegenerator.models import (
    AdminPonderSec,
    AvaliacaoFormulario,
    AvaliacaoPublicaLLM,
    Categoria,
    Formulario,
    LLM,
    LLMPublica,
    Metrica,
    PerguntaPublica,
    Questao,
    Resposta,
    RespostaPublica,
)
from responsegenerator.views import ADMIN_SESSION_KEY


class URLConfigurationTests(SimpleTestCase):
    def test_routes_and_names_are_unique(self):
        routes = [
            str(pattern.pattern) for pattern in responsegenerator_urls.urlpatterns
        ]
        names = [
            pattern.name
            for pattern in responsegenerator_urls.urlpatterns
            if pattern.name
        ]

        self.assertEqual(
            [route for route, count in Counter(routes).items() if count > 1], []
        )
        self.assertEqual(
            [name for name, count in Counter(names).items() if count > 1], []
        )

    def test_llm_edit_api_has_a_reversible_name(self):
        self.assertEqual(reverse("edit_llm_api", args=[7]), "/api/llm/7/edit/")


class PublicChatPageTests(TestCase):
    def test_new_messages_use_minimum_automatic_scroll(self):
        response = self.client.get(reverse("usuario_final_chat"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "messageElement.scrollIntoView")
        self.assertContains(response, "block: 'nearest'")
        self.assertNotContains(response, "chatHistory.scrollHeight")


class PublicQualitativeEvaluationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="researcher", password="secret")
        category = Categoria.objects.create(
            usuario=self.owner,
            nome_categoria="Phishing",
        )
        question = Questao.objects.create(
            usuario=self.owner,
            categoria=category,
            conteudo="Como identificar phishing?",
        )
        llm = LLM.objects.create(
            usuario=self.owner,
            nome="Modelo A",
            api_key="key-a",
        )
        self.answer = Resposta.objects.create(
            questao=question,
            llm=llm,
            conteudo_resposta="Verifique o remetente e os links.",
        )
        self.form = Formulario.objects.create(nome="Avaliação", usuario=self.owner)
        self.form.questoes.add(question)
        self.quantitative_metric = Metrica.objects.create(
            usuario=self.owner,
            nome="Clareza",
            descricao="Avalie a clareza.",
            tipo="quantitativa",
            pontuacao_maxima=5,
            ativa=True,
        )
        self.qualitative_metric = Metrica.objects.create(
            usuario=self.owner,
            nome="Observações",
            descricao="Descreva pontos fortes e limitações.",
            tipo="qualitativa",
            ativa=True,
        )
        self.url = reverse("responder_avaliacao_publica", args=[self.form.id])

    def test_quantitative_and_qualitative_metrics_render_together(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="slide-metrics metric-count-2"')
        self.assertContains(response, 'data-metric-count="2"')
        self.assertContains(response, 'class="metric-block quantitative"')
        self.assertContains(response, 'class="metric-block qualitative"')
        self.assertContains(response, 'class="likert-drag"')
        self.assertContains(
            response,
            f'name="quanti_{self.answer.id}_{self.quantitative_metric.id}"',
        )
        self.assertContains(response, 'class="comment-field qualitative-comment-field"')
        self.assertContains(
            response,
            f'name="quali_{self.answer.id}_{self.qualitative_metric.id}"',
        )
        self.assertNotContains(
            response,
            f'name="quanti_{self.answer.id}_{self.qualitative_metric.id}"',
        )

    def test_qualitative_comment_is_saved_without_numeric_score(self):
        response = self.client.post(
            self.url,
            data={
                "nome": "Ana",
                "email": "ana@example.com",
                "profissao": "Analista",
                f"quanti_{self.answer.id}_{self.quantitative_metric.id}": "4",
                f"quali_{self.answer.id}_{self.quantitative_metric.id}": "Resposta clara.",
                f"quali_{self.answer.id}_{self.qualitative_metric.id}": "Faltou mencionar URLs encurtadas.",
            },
        )

        self.assertEqual(response.status_code, 200)
        quantitative = AvaliacaoFormulario.objects.get(metrica=self.quantitative_metric)
        qualitative = AvaliacaoFormulario.objects.get(metrica=self.qualitative_metric)
        self.assertEqual(quantitative.avaliacao_quanti, 4)
        self.assertEqual(quantitative.avaliacao_quali, "Resposta clara.")
        self.assertIsNone(qualitative.avaliacao_quanti)
        self.assertEqual(
            qualitative.avaliacao_quali, "Faltou mencionar URLs encurtadas."
        )


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
                return json.dumps(
                    {
                        "notas": [
                            {
                                "metrica": "Clareza",
                                "nota": 4,
                                "justificativa": "Clara para público leigo.",
                            }
                        ],
                        "justificativa": "Resposta adequada.",
                    }
                )
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

        metricas_response = self.client.get(
            reverse("admin_pondersec_metricas_publicas")
        )
        avaliacoes_response = self.client.get(
            reverse("admin_pondersec_avaliacoes_publicas")
        )

        self.assertEqual(metricas_response.status_code, 200)
        self.assertEqual(avaliacoes_response.status_code, 200)
