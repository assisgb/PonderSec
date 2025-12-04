from django.shortcuts import render

# Create your views here.
def tela_inicial(request):
    return render(request, 'tela_inicial.html')