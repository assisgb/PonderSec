from google import genai
from groq import Groq
import os

def gerar_respostas(perguntas: str) -> dict:
    client_gemini = genai.Client(
        api_key=os.environ.get("GOOGLE_API_KEY")
    )

    client_groq = Groq(
        api_key=os.environ.get("GROQ_API_KEY")
    )

    resposta_gemini = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=perguntas
    )

    resposta_groq = client_groq.chat.completions.create(
        model="llama3-8b-8192",
        messages=[{"role": "user", "content": perguntas}]
    )

    return {
        "gemini": resposta_gemini.text,
        "groq": resposta_groq.choices[0].message["content"]
    }
