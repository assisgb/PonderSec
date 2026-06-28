from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse


class LoginRedirectTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="researcher",
            password="safe-test-password",
            is_active=True,
        )

    def test_login_accepts_internal_next_url(self):
        response = self.client.post(
            f"{reverse('login')}?next={reverse('setup')}",
            {"username": self.user.username, "password": "safe-test-password"},
        )

        self.assertRedirects(response, reverse("setup"), fetch_redirect_response=False)

    def test_login_rejects_external_next_url(self):
        response = self.client.post(
            f"{reverse('login')}?next=https://example.invalid/phishing",
            {"username": self.user.username, "password": "safe-test-password"},
        )

        self.assertRedirects(
            response, reverse("questoes"), fetch_redirect_response=False
        )
