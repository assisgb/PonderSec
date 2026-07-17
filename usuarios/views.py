import logging
import secrets

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from usuarios.models import CodigoVerificacao


logger = logging.getLogger(__name__)

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60  # 15 minutos
PENDING_USER_SESSION_KEY = "usuario_inativo_id"


def _login_cache_key(username):
    return f"login_attempts_{username.lower()}"


def _get_client_ip(request):
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _login_ip_cache_key(ip):
    return f"login_attempts_ip_{ip}"


def _new_verification_code():
    """Gera um código de seis dígitos com fonte criptograficamente segura."""
    return str(secrets.randbelow(900000) + 100000)


def _clear_pending_registration(request, *, delete_inactive_user=False):
    """Desvincula a sessão do cadastro anterior e, se pedido, remove o rascunho."""
    user_id = request.session.pop(PENDING_USER_SESSION_KEY, None)
    if not user_id or not delete_inactive_user:
        return

    deleted, _ = User.objects.filter(pk=user_id, is_active=False).delete()
    if deleted:
        logger.info("Cadastro inativo anterior removido ao iniciar um novo cadastro.")


def _get_pending_user(request):
    """Retorna somente o usuário inativo e com código pertencente à sessão atual."""
    user_id = request.session.get(PENDING_USER_SESSION_KEY)
    if not user_id:
        return None

    try:
        user = User.objects.get(pk=user_id, is_active=False)
    except User.DoesNotExist:
        request.session.pop(PENDING_USER_SESSION_KEY, None)
        logger.warning("Sessão de cadastro apontava para um usuário inexistente ou já ativo.")
        return None

    if not CodigoVerificacao.objects.filter(usuario=user).exists():
        request.session.pop(PENDING_USER_SESSION_KEY, None)
        user.delete()
        logger.warning("Cadastro inativo sem código foi descartado.")
        return None

    return user


def _verification_url():
    return f"{reverse('cadastro')}?verificacao=1"


def cadastro(request):
    if request.method == "GET":
        if request.GET.get("verificacao") == "1":
            user = _get_pending_user(request)
            if user:
                return render(
                    request,
                    "cadastro.html",
                    {"precisa_verificar": True, "email_user": user.email},
                )
            messages.error(
                request,
                _("Sessão de verificação expirada. Inicie o cadastro novamente."),
            )
        else:
            # A URL normal de cadastro sempre começa um fluxo novo. Isso impede que
            # um ID antigo da sessão seja apresentado como se um novo código tivesse
            # acabado de ser enviado.
            _clear_pending_registration(request, delete_inactive_user=True)

        return render(request, "cadastro.html")

    if "cancelar_verificacao" in request.POST:
        _clear_pending_registration(request, delete_inactive_user=True)
        messages.info(request, _("Cadastro anterior cancelado. Você pode começar novamente."))
        return redirect("cadastro")

    if "codigo_input" in request.POST:
        user = _get_pending_user(request)
        if not user:
            return render(
                request,
                "cadastro.html",
                {"erro": _("Sessão expirada. Inicie o cadastro novamente.")},
            )

        codigo_digitado = request.POST.get("codigo_input", "").strip()
        try:
            registro = CodigoVerificacao.objects.get(
                usuario=user,
                codigo=codigo_digitado,
            )
        except CodigoVerificacao.DoesNotExist:
            return render(
                request,
                "cadastro.html",
                {
                    "precisa_verificar": True,
                    "email_user": user.email,
                    "erro": _("Código inválido. Tente novamente."),
                },
            )

        with transaction.atomic():
            user.is_active = True
            user.save(update_fields=["is_active"])
            registro.delete()

        _clear_pending_registration(request)
        messages.success(request, _("Conta ativada com sucesso! Faça login para acessar."))
        return redirect("login")

    if "username" in request.POST:
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip().lower()
        senha = request.POST.get("password", "")
        senha_confirm = request.POST.get("password_confirm", "")

        if not username or not email or not senha:
            return render(
                request,
                "cadastro.html",
                {"erro": _("Preencha nome de usuário, e-mail e senha.")},
            )

        if senha != senha_confirm:
            return render(
                request,
                "cadastro.html",
                {"erro": _("As senhas não coincidem!")},
            )

        # Um POST de cadastro novo nunca pode herdar o destinatário de um fluxo
        # anterior aberto no mesmo navegador.
        _clear_pending_registration(request, delete_inactive_user=True)

        if User.objects.filter(email__iexact=email).exists():
            return render(
                request,
                "cadastro.html",
                {"erro": _("E-mail já cadastrado!")},
            )

        if User.objects.filter(username__iexact=username).exists():
            return render(
                request,
                "cadastro.html",
                {"erro": _("Nome de usuário já existe!")},
            )

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=senha,
                    is_active=False,
                )
                codigo_secreto = _new_verification_code()
                CodigoVerificacao.objects.create(
                    usuario=user,
                    codigo=codigo_secreto,
                )

                sent_count = send_mail(
                    _("Código de Verificação - PonderSec"),
                    _(
                        "Seu código de ativação é: %(codigo)s\n\n"
                        "Se você não solicitou este código, ignore este e-mail."
                    )
                    % {"codigo": codigo_secreto},
                    None,
                    [email],
                    fail_silently=False,
                )
                if sent_count != 1:
                    raise RuntimeError("O backend de e-mail não confirmou o envio.")
        except Exception:
            # A transação também remove usuário/código recém-criados, permitindo
            # uma nova tentativa com o mesmo e-mail.
            request.session.pop(PENDING_USER_SESSION_KEY, None)
            logger.exception("Falha ao criar cadastro e enviar o código de verificação.")
            return render(
                request,
                "cadastro.html",
                {
                    "erro": _(
                        "Não foi possível enviar o código de verificação. "
                        "Tente novamente em alguns instantes."
                    )
                },
                status=503,
            )

        request.session[PENDING_USER_SESSION_KEY] = user.id
        return redirect(_verification_url())

    return render(
        request,
        "cadastro.html",
        {"erro": _("Dados de cadastro inválidos.")},
        status=400,
    )


def verificar_codigo(request):
    """Compatibilidade com o fluxo antigo; a verificação principal usa cadastro."""
    user = _get_pending_user(request)
    if not user:
        return redirect("cadastro")

    if request.method == "POST":
        codigo_digitado = request.POST.get("codigo_input", "").strip()
        try:
            registro = CodigoVerificacao.objects.get(
                usuario=user,
                codigo=codigo_digitado,
            )
        except CodigoVerificacao.DoesNotExist:
            return HttpResponse(_("Código inválido ou expirado. Tente novamente."))

        with transaction.atomic():
            user.is_active = True
            user.save(update_fields=["is_active"])
            registro.delete()
        _clear_pending_registration(request)
        return redirect("login")

    return render(request, "verificar_codigo.html")


def reenviar_codigo(request):
    # Enviar e-mail é um efeito colateral: GET nunca deve dispará-lo. Isso também
    # evita reenvios causados por prefetch do navegador ou robôs.
    if request.method != "POST":
        user = _get_pending_user(request)
        return redirect(_verification_url() if user else "cadastro")

    user = _get_pending_user(request)
    if not user:
        messages.error(
            request,
            _("Sessão expirada. Por favor, inicie o cadastro novamente."),
        )
        return redirect("cadastro")

    try:
        with transaction.atomic():
            registro = CodigoVerificacao.objects.select_for_update().get(usuario=user)
            codigo_secreto = _new_verification_code()
            registro.codigo = codigo_secreto
            registro.save(update_fields=["codigo"])

            sent_count = send_mail(
                _("Novo Código de Verificação - PonderSec"),
                _(
                    "Seu novo código de ativação é: %(codigo)s\n\n"
                    "Se você não solicitou este código, ignore este e-mail."
                )
                % {"codigo": codigo_secreto},
                None,
                [user.email],
                fail_silently=False,
            )
            if sent_count != 1:
                raise RuntimeError("O backend de e-mail não confirmou o reenvio.")
    except Exception:
        logger.exception("Falha ao reenviar código de verificação.")
        messages.error(
            request,
            _(
                "Não foi possível reenviar o código. "
                "O código anterior continua válido; tente novamente mais tarde."
            ),
        )
        return redirect(_verification_url())

    messages.success(request, _("Um novo código foi enviado para o seu e-mail!"))
    return redirect(_verification_url())


def login_view(request):
    if request.method == "GET":
        return render(request, "login.html")

    username = request.POST.get("username", "").strip()
    senha = request.POST.get("password", "")
    ip = _get_client_ip(request)

    user_key = _login_cache_key(username)
    ip_key = _login_ip_cache_key(ip)

    user_attempts = cache.get(user_key, 0)
    ip_attempts = cache.get(ip_key, 0)

    if user_attempts >= LOGIN_MAX_ATTEMPTS or ip_attempts >= LOGIN_MAX_ATTEMPTS:
        return render(
            request,
            "login.html",
            {
                "error": _(
                    "Conta bloqueada por excesso de tentativas. "
                    "Tente novamente em 15 minuto(s)."
                )
            },
        )

    user = authenticate(username=username, password=senha)

    if user:
        cache.delete(user_key)
        cache.delete(ip_key)
        login(request, user)
        next_url = request.GET.get("next")
        if next_url:
            return redirect(next_url)
        return redirect("questoes")

    new_user_attempts = user_attempts + 1
    new_ip_attempts = ip_attempts + 1
    cache.set(user_key, new_user_attempts, LOGIN_LOCKOUT_SECONDS)
    cache.set(ip_key, new_ip_attempts, LOGIN_LOCKOUT_SECONDS)

    remaining = LOGIN_MAX_ATTEMPTS - max(new_user_attempts, new_ip_attempts)

    if remaining <= 0:
        return render(
            request,
            "login.html",
            {
                "error": _(
                    "Conta bloqueada por excesso de tentativas. "
                    "Tente novamente em 15 minuto(s)."
                )
            },
        )

    return render(
        request,
        "login.html",
        {
            "error": _(
                "Usuário inválido ou conta não ativada. "
                "%(rem)d tentativa(s) restante(s)."
            )
            % {"rem": remaining}
        },
    )


def logout_view(request):
    logout(request)
    return redirect("login")
