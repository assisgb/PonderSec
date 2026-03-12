from django.db import models
from django.contrib.auth.models import User


class HistoricoAntigo(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    pergunta = models.TextField()
    resposta_gemini = models.TextField(blank=True, null=True)
    resposta_groq = models.TextField(blank=True, null=True)
    data = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-data']

    def __str__(self):
        return f"{self.usuario.username} - {self.data}"


class Metrica(models.Model):
    TIPO_CHOICES = [
        ('quantitativa', 'Quantitativa'),
        ('qualitativa', 'Qualitativa'),
    ]
    nome = models.CharField(max_length=100)
    descricao = models.TextField()
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    pontuacao_maxima = models.IntegerField(null=True, blank=True)
    criterio_texto = models.TextField(null=True, blank=True)
    ativa = models.BooleanField(default=True)

    def __str__(self):
        return self.nome


class LLM(models.Model):
    nome = models.CharField(max_length=100)
    descricao = models.TextField(blank=True, null=True)
    api_key = models.CharField(max_length=255)
    ativo = models.BooleanField(default=True)

    def __str__(self):
        return self.nome


class Categoria(models.Model):
    nome_categoria = models.CharField(max_length=100)
    descricao_categoria = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.nome_categoria




class Resposta(models.Model):
    conteudo_resposta = models.TextField()

    def __str__(self):
        return f"{self.questao} - {self.llm}"



class Questao(models.Model):
    conteudo = models.TextField()
    llm = models.ForeignKey(LLM, on_delete=models.SET_NULL, null=True)
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True)
    respostas = models.ForeignKey(Resposta, on_delete=models.SET_NULL, null=True)
    def __str__(self):
        return self.conteudo[:50]









class Avaliacao(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    resposta = models.ForeignKey(Resposta, on_delete=models.CASCADE)
    metrica = models.ForeignKey(Metrica, on_delete=models.SET_NULL, null=True)
    avaliacao_quali = models.TextField(blank=True, null=True)
    avaliacao_quanti = models.IntegerField(blank=True, null=True)

    def __str__(self):
        return f"{self.usuario} - {self.resposta}"


class Historico(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    questao = models.ForeignKey(Questao, on_delete=models.SET_NULL, null=True, blank=True)
    data = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-data']

    def __str__(self):
        return f"{self.usuario.username} - {self.data}"

class Formulario(models.Model):
    nome = models.CharField(max_length=200)
    questoes = models.ManyToManyField(Questao, blank=True)
    criado_por = models.ForeignKey(User, on_delete=models.CASCADE)

    def __str__(self):
        return self.nome