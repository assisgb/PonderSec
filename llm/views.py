
# Create your views here.
from django.shortcuts import render
from .services import gerar_respostas

def llm_home(request):
    contexto = {
        "prompt": "",
        "resposta_gemini": "",
        "resposta_groq": ""
    }

    if request.method == "POST":
        prompt = request.POST.get("prompt")
        respostas = gerar_respostas(prompt)

        contexto["prompt"] = prompt
        contexto["resposta_gemini"] = respostas["gemini"]
        contexto["resposta_groq"] = respostas["groq"]

    return render(request, "llm/llm_home.html", contexto)
