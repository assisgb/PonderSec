import random
from django.contrib import messages
from django.shortcuts import redirect, render
from django.core.mail import send_mail
from usuarios.models import CodigoVerificacao
from django.http import HttpResponse
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.utils.translation import gettext as _

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

def verificar_codigo(request):
    user_id = request.session.get('usuario_inativo_id')
    
    if not user_id:
        return redirect('login')

    if request.method == "POST":
        codigo_digitado = request.POST.get('codigo_input').strip()
        
        try:
            registro = CodigoVerificacao.objects.get(usuario_id=user_id, codigo=codigo_digitado)
            user = registro.usuario
            user.is_active = True
            user.save()
            
            registro.delete()
            del request.session['usuario_inativo_id']
            
            return redirect('login')
            
        except CodigoVerificacao.DoesNotExist:
            return HttpResponse(_("Código inválido ou expirado. Tente novamente."))

    return render(request, 'verificar_codigo.html')


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
    else:
        username = request.POST.get('username')
        senha = request.POST.get('password')
        user = authenticate(username=username, password=senha)

        if user:
            login(request, user)
            # 🔹 respeita ?next= se existir
            next_url = request.GET.get('next')

            if next_url:
                return redirect(next_url)

            return redirect('questoes')

        return render(request, 'login.html', {
            'error': _('Usuário inválido ou conta não ativada')
        })

def logout_view(request):
    logout(request)
    return redirect('login')
