from django.shortcuts import render
from django.http import HttpResponse
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate

# Create your views here.
def cadastro(request):
    if request.method == "GET":
        return render(request, 'cadastro.html')
    else:
        username = request.POST.get('username')
        email = request.POST.get('email')
        senha = request.POST.get('password')
        senha_confirm = request.POST.get('password_confirm')

        if senha != senha_confirm:
            return HttpResponse("As senhas não coincidem!");
    
        user = User.objects.filter(username=username).first()

        if user:
            return HttpResponse("Usuário já existe");

        user = User.objects.create_user(username=username, email=email, password=senha)
        user.save()
        return HttpResponse("Usuário cadastrado com sucesso!");

def login(request):
    if request.method == "GET":
        return render(request, 'login.html')
    else:
    
        username = request.POST.get('username')
        senha = request.POST.get('password')
        user = authenticate(username=username, password=senha)

        if user:
            login(request, user)
            return HttpResponse("Login realizado com sucesso!");
        else:
            return HttpResponse("Credenciais inválidas!");


