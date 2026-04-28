import random
from django.shortcuts import redirect, render
from django.core.mail import send_mail
from usuarios.models import CodigoVerificacao
from django.http import HttpResponse
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout

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
                
                
                return redirect('login', {
                    'sucesso': 'Conta ativada com sucesso! Faça login para acessar.'
                })
                
            except CodigoVerificacao.DoesNotExist:
                return render(request, 'cadastro.html', {
                    'precisa_verificar': True, 
                    'erro': 'Código inválido. Tente novamente.'
                })

        elif 'username' in request.POST:
            username = request.POST.get('username')
            email = request.POST.get('email')
            senha = request.POST.get('password')
            senha_confirm = request.POST.get('password_confirm')

            if senha != senha_confirm:
                return render(request, 'cadastro.html', {'erro': 'As senhas não coincidem!'})
        
            if User.objects.filter(username=username, email=email).exists():
                return render(request, 'cadastro.html', {'erro': 'Usuário já existe!'})

            user = User.objects.create_user(username=username, email=email, password=senha)
            user.is_active = False 
            user.save()

            codigo_secreto = str(random.randint(100000, 999999))
            CodigoVerificacao.objects.create(usuario=user, codigo=codigo_secreto)

            # Envia e-mail
            send_mail(
                "Código de Verificação - PonderSec",
                f"Seu código de ativação é: {codigo_secreto}\n\n Se você não solicitou este código, ignore este e-mail.",
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
            return HttpResponse("Código inválido ou expirado. Tente novamente.")

    return render(request, 'verificar_codigo.html')


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

            return redirect('menu')

        return render(request, 'login.html', {
            'error': 'Usuário inválido ou conta não ativada'
        })

def logout_view(request):
    logout(request)
    return redirect('login')