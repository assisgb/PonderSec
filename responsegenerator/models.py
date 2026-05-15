from django.db import models
from django.contrib.auth.models import User


class Metrica(models.Model):
    TIPO_CHOICES = [
        ('quantitativa', 'Quantitativa'),
        ('qualitativa', 'Qualitativa'),
    ]
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    nome = models.CharField(max_length=100)
    descricao = models.TextField()
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    pontuacao_maxima = models.IntegerField(null=True, blank=True)
    criterio_texto = models.TextField(null=True, blank=True)
    label_opcao_1 = models.CharField(max_length=50, blank=True, null=True)
    label_opcao_2 = models.CharField(max_length=50, blank=True, null=True)
    ativa = models.BooleanField(default=True)

    def __str__(self):
        return self.nome


class LLM(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    nome = models.CharField(max_length=100)
    descricao = models.TextField(blank=True, null=True)
    api_key = models.CharField(max_length=255)
    ativo = models.BooleanField(default=True)

    def __str__(self):
        return self.nome


class Categoria(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    nome_categoria = models.CharField(max_length=100)
    descricao_categoria = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ('usuario', 'nome_categoria')

    def __str__(self):
        return self.nome_categoria


class Questao(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    conteudo = models.TextField()
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return self.conteudo[:50]


class Resposta(models.Model):
    questao = models.ForeignKey(
        Questao,
        on_delete=models.CASCADE,
        related_name="respostas",
        null=True,
        blank=True
    )

    llm = models.ForeignKey(
        LLM,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )

    conteudo_resposta = models.TextField()

    def __str__(self):
        return f"{self.questao} - {self.llm}"


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
    questoes = models.ManyToManyField(Questao, blank=True, related_name='formularios')
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self):
        return self.nome


class Avaliador(models.Model):
    nome = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    profissao = models.CharField(max_length=100, blank=True, null=True)
    formulario = models.ForeignKey(Formulario, on_delete=models.CASCADE, related_name='avaliadores')
    data_resposta = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.nome} - {self.email}"


class AvaliacaoFormulario(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    avaliador = models.ForeignKey(Avaliador, on_delete=models.CASCADE, null=True, blank=True, related_name='avaliacoes')

    resposta = models.ForeignKey(Resposta, on_delete=models.CASCADE)
    metrica = models.ForeignKey(Metrica, on_delete=models.SET_NULL, null=True)
    avaliacao_quali = models.TextField(blank=True, null=True)
    avaliacao_quanti = models.IntegerField(blank=True, null=True)

    def __str__(self):
        nome_valiador = self.usuario.username if self.usuario else self.avaliador.nome
        return f"{nome_valiador} - {self.resposta}"


class AvaliacaoJuiz(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    juiz = models.ForeignKey(LLM, on_delete=models.SET_NULL, null=True, blank=True, related_name='avaliacoes_como_juiz')
    resposta = models.ForeignKey(Resposta, on_delete=models.CASCADE, related_name='avaliacoes_juizes')
    metrica = models.ForeignKey(Metrica, on_delete=models.SET_NULL, null=True)
    avaliacao_quali = models.TextField(blank=True, null=True)
    avaliacao_quanti = models.IntegerField(blank=True, null=True)
    justificativa_geral = models.TextField(blank=True, null=True)
    erro = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-atualizado_em']
        unique_together = ('usuario', 'juiz', 'resposta', 'metrica')

    def __str__(self):
        nome_juiz = self.juiz.nome if self.juiz else 'Juiz removido'
        return f"{nome_juiz} avaliou {self.resposta}"
