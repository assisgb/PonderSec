from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from usuarios.models import CodigoVerificacao
from usuarios.views import PENDING_USER_SESSION_KEY


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CadastroTestCase(TestCase):
    def _registration_data(self, **overrides):
        data = {
            "username": "novo.usuario",
            "email": "novo@example.com",
            "password": "uma-senha-segura",
            "password_confirm": "uma-senha-segura",
        }
        data.update(overrides)
        return data

    def _pending_user(self, username="pendente", email="pendente@example.com", code="123456"):
        user = User.objects.create_user(
            username=username,
            email=email,
            password="uma-senha-segura",
            is_active=False,
        )
        CodigoVerificacao.objects.create(usuario=user, codigo=code)
        session = self.client.session
        session[PENDING_USER_SESSION_KEY] = user.pk
        session.save()
        return user

    def test_new_registration_never_sends_code_to_previous_pending_user(self):
        previous_user = self._pending_user()

        response = self.client.post(reverse("cadastro"), self._registration_data())

        self.assertRedirects(
            response,
            f"{reverse('cadastro')}?verificacao=1",
            fetch_redirect_response=False,
        )
        self.assertFalse(User.objects.filter(pk=previous_user.pk).exists())
        new_user = User.objects.get(username="novo.usuario")
        self.assertFalse(new_user.is_active)
        self.assertEqual(self.client.session[PENDING_USER_SESSION_KEY], new_user.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["novo@example.com"])

    def test_plain_registration_get_discards_stale_pending_flow_without_sending_email(self):
        previous_user = self._pending_user()

        response = self.client.get(reverse("cadastro"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "codigo_input")
        self.assertFalse(User.objects.filter(pk=previous_user.pk).exists())
        self.assertNotIn(PENDING_USER_SESSION_KEY, self.client.session)
        self.assertEqual(len(mail.outbox), 0)

    def test_verification_page_shows_only_session_user_email_without_sending(self):
        user = self._pending_user(email="destino@example.com")
        User.objects.create_user(
            username="ultimo",
            email="ultimo@example.com",
            password="uma-senha-segura",
        )

        response = self.client.get(f"{reverse('cadastro')}?verificacao=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, user.email)
        self.assertNotContains(response, "ultimo@example.com")
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_requires_post_and_targets_only_pending_user(self):
        user = self._pending_user(email="destino@example.com", code="123456")
        User.objects.create_user(
            username="ultimo",
            email="ultimo@example.com",
            password="uma-senha-segura",
        )

        get_response = self.client.get(reverse("reenviar_codigo"))
        self.assertRedirects(
            get_response,
            f"{reverse('cadastro')}?verificacao=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(CodigoVerificacao.objects.get(usuario=user).codigo, "123456")

        with mock.patch("usuarios.views._new_verification_code", return_value="654321"):
            post_response = self.client.post(reverse("reenviar_codigo"))

        self.assertRedirects(
            post_response,
            f"{reverse('cadastro')}?verificacao=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["destino@example.com"])
        self.assertEqual(CodigoVerificacao.objects.get(usuario=user).codigo, "654321")

    @mock.patch("usuarios.views.send_mail", side_effect=OSError("SMTP indisponível"))
    def test_email_failure_is_visible_and_rolls_back_user_and_code(self, mocked_send):
        with self.assertLogs("usuarios.views", level="ERROR"):
            response = self.client.post(reverse("cadastro"), self._registration_data())

        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "Não foi possível enviar", status_code=503)
        self.assertFalse(User.objects.filter(username="novo.usuario").exists())
        self.assertEqual(CodigoVerificacao.objects.count(), 0)
        self.assertNotIn(PENDING_USER_SESSION_KEY, self.client.session)
        mocked_send.assert_called_once()

    def test_code_is_bound_to_pending_user_from_session(self):
        other = User.objects.create_user(
            username="outro",
            email="outro@example.com",
            password="uma-senha-segura",
            is_active=False,
        )
        CodigoVerificacao.objects.create(usuario=other, codigo="111111")
        pending = self._pending_user(code="222222")

        wrong_response = self.client.post(
            reverse("cadastro"),
            {"codigo_input": "111111"},
        )
        self.assertEqual(wrong_response.status_code, 200)
        other.refresh_from_db()
        pending.refresh_from_db()
        self.assertFalse(other.is_active)
        self.assertFalse(pending.is_active)

        success_response = self.client.post(
            reverse("cadastro"),
            {"codigo_input": "222222"},
        )
        self.assertRedirects(success_response, reverse("login"), fetch_redirect_response=False)
        pending.refresh_from_db()
        self.assertTrue(pending.is_active)
        self.assertFalse(CodigoVerificacao.objects.filter(usuario=pending).exists())
        self.assertNotIn(PENDING_USER_SESSION_KEY, self.client.session)

    @mock.patch("usuarios.views.send_mail", side_effect=TimeoutError("SMTP timeout"))
    def test_resend_failure_keeps_previous_code_and_shows_message(self, mocked_send):
        user = self._pending_user(code="123456")

        with self.assertLogs("usuarios.views", level="ERROR"):
            with mock.patch("usuarios.views._new_verification_code", return_value="654321"):
                response = self.client.post(reverse("reenviar_codigo"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "O código anterior continua válido")
        self.assertEqual(CodigoVerificacao.objects.get(usuario=user).codigo, "123456")
        mocked_send.assert_called_once()
