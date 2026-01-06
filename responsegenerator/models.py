from django.db import models
from django.contrib.auth.models import User

class Historico(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE) # => Vincula o histórico ao usuário logado
    pergunta = models.TextField()
    resposta_gemini = models.TextField(blank=True, null=True)
    resposta_groq = models.TextField(blank=True, null=True)
    data = models.DateTimeField(auto_now_add=True)

    class Meta:  
        ordering = ['-data'] # => Aqui ordena do mais recente para o mais antigo

    def __str__(self):
        return f"{self.usuario.username} - {self.data}"