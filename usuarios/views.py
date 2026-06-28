import random

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib import messages
from django.core.cache import cache
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

from usuarios.models import CodigoVerificacao

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60  # 15 minutos


def _login_cache_key(username):
    return f"login_attempts_{username.lower()}"


def _get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _login_ip_cache_key(ip):
    return f"login_attempts_ip_{ip}"

def cadastro(request):
    user_id = request.session.get('usuario_inativo_id')

    if request.method == "POST":
        
        if 'codigo_input' in request.POST:
            codigo_digitado = request.POST.get('codigo_input').strip()
            
            try:
                registro = CodigoVerificacao.objects.get(usuario_id=user_id, codigo=codigo_digitado)
                user = registro.usuario
                user.is_active = True
                user.save()
                
                registro.delete()
                del request.session['usuario_inativo_id']

                messages.success(request, _('Conta ativada com sucesso! Faça login para acessar.'))
                
                return redirect('login')
                
            except CodigoVerificacao.DoesNotExist:
                return render(request, 'cadastro.html', {
                    'precisa_verificar': True, 
                    'erro': _('Código inválido. Tente novamente.')
                })

        elif 'username' in request.POST:
            username = request.POST.get('username')
            email = request.POST.get('email')
            senha = request.POST.get('password')
            senha_confirm = request.POST.get('password_confirm')

            if senha != senha_confirm:
                return render(request, 'cadastro.html', {'erro': _('As senhas não coincidem!')})
        
            if User.objects.filter(email=email).exists():
                return render(request, 'cadastro.html', {'erro': _('E-mail já cadastrado!')})
            
            if User.objects.filter(username=username).exists():
                return render(request, 'cadastro.html', {'erro': _('Nome de usuário já existe!')})

            user = User.objects.create_user(username=username, email=email, password=senha)
            user.is_active = False 
            user.save()

            codigo_secreto = str(random.randint(100000, 999999))
            CodigoVerificacao.objects.create(usuario=user, codigo=codigo_secreto)

            # Envia e-mail
            send_mail(
                _("Código de Verificação - PonderSec"),
                _("Seu código de ativação é: %(codigo)s\n\nSe você não solicitou este código, ignore este e-mail.") % {
                    "codigo": codigo_secreto
                },
                None,
                [user.email],
                fail_silently=False,
            )

            request.session['usuario_inativo_id'] = user.id
            return render(request, 'cadastro.html', {
                'precisa_verificar': True, 
                'email_user': user.email 
            })

    if user_id:
        return render(request, 'cadastro.html', {'precisa_verificar': True})

    return render(request, 'cadastro.html')

def reenviar_codigo(request):
    user_id = request.session.get('usuario_inativo_id')
    
    if not user_id:
        messages.error(request, _('Sessão expirada. Por favor, inicie o cadastro novamente.'))
        return redirect('cadastro')
        
    try:
        user = User.objects.get(id=user_id)
        
        CodigoVerificacao.objects.filter(usuario=user).delete()

        codigo_secreto = str(random.randint(100000, 999999))
        CodigoVerificacao.objects.create(usuario=user, codigo=codigo_secreto)
        
        send_mail(
            _("Novo Código de Verificação - PonderSec"),
            _("Seu novo código de ativação é: %(codigo)s\n\nSe você não solicitou este código, ignore este e-mail.") % {
                "codigo": codigo_secreto
            },
            None,
            [user.email],
            fail_silently=False,
        )
        
        messages.success(request, _('Um novo código foi enviado para o seu e-mail!'))
        
    except User.DoesNotExist:
        messages.error(request, _('Usuário não encontrado no sistema.'))
        del request.session['usuario_inativo_id']
        return redirect('cadastro')

    return redirect('cadastro')


def login_view(request):
    if request.method == "GET":
        return render(request, 'login.html')

    username = request.POST.get('username', '').strip()
    senha = request.POST.get('password', '')
    ip = _get_client_ip(request)

    user_key = _login_cache_key(username)
    ip_key = _login_ip_cache_key(ip)

    user_attempts = cache.get(user_key, 0)
    ip_attempts = cache.get(ip_key, 0)

    if user_attempts >= LOGIN_MAX_ATTEMPTS or ip_attempts >= LOGIN_MAX_ATTEMPTS:
        return render(request, 'login.html', {
            'error': _('Conta bloqueada por excesso de tentativas. Tente novamente em 15 minuto(s).')
        })

    user = authenticate(username=username, password=senha)

    if user:
        cache.delete(user_key)
        cache.delete(ip_key)
        login(request, user)
        next_url = request.GET.get('next')
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)
        return redirect('questoes')

    new_user_attempts = user_attempts + 1
    new_ip_attempts = ip_attempts + 1
    cache.set(user_key, new_user_attempts, LOGIN_LOCKOUT_SECONDS)
    cache.set(ip_key, new_ip_attempts, LOGIN_LOCKOUT_SECONDS)

    remaining = LOGIN_MAX_ATTEMPTS - max(new_user_attempts, new_ip_attempts)

    if remaining <= 0:
        return render(request, 'login.html', {
            'error': _('Conta bloqueada por excesso de tentativas. Tente novamente em 15 minuto(s).')
        })

    return render(request, 'login.html', {
        'error': _('Usuário inválido ou conta não ativada. %(rem)d tentativa(s) restante(s).') % {'rem': remaining}
    })

def logout_view(request):
    logout(request)
    return redirect('login')
