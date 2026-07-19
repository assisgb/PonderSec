from django.db import models
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.utils import timezone


class AdminPonderSec(models.Model):
    """Admin do painel /admin-pondersec/ — auth separada dos User (pesquisadores)."""
    nome = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    senha_hash = models.CharField(max_length=128)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    ultimo_acesso = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Admin PonderSec"
        verbose_name_plural = "Admins PonderSec"

    def __str__(self):
        return f"{self.nome} <{self.email}>"

    def set_senha(self, senha_em_texto):
        self.senha_hash = make_password(senha_em_texto)

    def verificar_senha(self, senha_em_texto):
        return check_password(senha_em_texto, self.senha_hash)

    def registrar_acesso(self):
        self.ultimo_acesso = timezone.now()
        self.save(update_fields=["ultimo_acesso"])


class LLMPublica(models.Model):
    """LLMs configuradas pelo admin para uso no chat público (não pertencem a pesquisadores)."""
    nome = models.CharField(max_length=100)
    descricao = models.TextField(blank=True, null=True)
    api_key = models.CharField(max_length=255)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    criado_por = models.ForeignKey(
        AdminPonderSec,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="llms_criadas",
    )

    class Meta:
        verbose_name = "LLM Pública"
        verbose_name_plural = "LLMs Públicas"
        ordering = ["-id"]

    def __str__(self):
        return self.nome


class PerguntaPublica(models.Model):
    """Perguntas feitas por usuários finais no chat público."""
    conteudo = models.TextField()
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Pergunta Pública"
        verbose_name_plural = "Perguntas Públicas"
        ordering = ["-criado_em"]
        indexes = [
            models.Index(fields=["-criado_em"], name="perg_pub_created_idx"),
        ]

    def __str__(self):
        return self.conteudo[:80]


class RespostaPublica(models.Model):
    """Respostas geradas por LLMs públicas para uma pergunta do chat público."""
    AVALIACAO_PENDENTE = "pendente"
    AVALIACAO_PROCESSANDO = "processando"
    AVALIACAO_CONCLUIDA = "concluida"
    AVALIACAO_ESTADOS = [
        (AVALIACAO_PENDENTE, "Pendente"),
        (AVALIACAO_PROCESSANDO, "Processando"),
        (AVALIACAO_CONCLUIDA, "Concluída"),
    ]

    pergunta = models.ForeignKey(
        PerguntaPublica,
        on_delete=models.CASCADE,
        related_name="respostas",
    )
    llm = models.ForeignKey(
        LLMPublica,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="respostas_publicas",
    )
    conteudo_resposta = models.TextField()
    ok = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    avaliacao_estado = models.CharField(
        max_length=12,
        choices=AVALIACAO_ESTADOS,
        default=AVALIACAO_PENDENTE,
    )
    avaliacao_iniciada_em = models.DateTimeField(null=True, blank=True)
    avaliacao_claim_id = models.UUIDField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Resposta Pública"
        verbose_name_plural = "Respostas Públicas"
        ordering = ["id"]

    def __str__(self):
        nome_llm = self.llm.nome if self.llm else "LLM removida"
        return f"{nome_llm} - {self.pergunta}"


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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("usuario", "nome"),
                condition=models.Q(usuario__isnull=False),
                name="unique_metric_user_name",
            ),
            models.UniqueConstraint(
                fields=("nome",),
                condition=models.Q(usuario__isnull=True),
                name="unique_global_metric_name",
            ),
        ]

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
    resposta_humana = models.TextField(blank=True, null=True, verbose_name="Resposta Humana")

    class Meta:
        indexes = [
            models.Index(fields=["usuario", "-id"], name="quest_user_id_idx"),
        ]

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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("questao", "llm"),
                condition=models.Q(llm__isnull=False),
                name="unique_resposta_questao_llm",
            ),
        ]

    def __str__(self):
        return f"{self.questao} - {self.llm}"


class Avaliacao(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    resposta = models.ForeignKey(Resposta, on_delete=models.CASCADE)
    metrica = models.ForeignKey(Metrica, on_delete=models.SET_NULL, null=True)
    avaliacao_quali = models.TextField(blank=True, null=True)
    avaliacao_quanti = models.IntegerField(blank=True, null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(avaliacao_quanti__isnull=True)
                    | models.Q(avaliacao_quanti__gte=1, avaliacao_quanti__lte=5)
                ),
                name="research_score_between_1_and_5",
            ),
        ]

    def __str__(self):
        return f"{self.usuario} - {self.resposta}"


class Historico(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    questao = models.ForeignKey(Questao, on_delete=models.SET_NULL, null=True, blank=True)
    data = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-data']
        indexes = [
            models.Index(fields=["usuario", "-data"], name="histor_user_data_idx"),
        ]

    def __str__(self):
        return f"{self.usuario.username} - {self.data}"


class Formulario(models.Model):
    TIPO_RESPOSTAS_CHOICES = [
        ('todas', 'Todas'),
        ('humana', 'Apenas Humana'),
        ('ia', 'Apenas IA'),
        ('ambas', 'IA + Humana'),
    ]

    nome = models.CharField(max_length=200)
    questoes = models.ManyToManyField(Questao, blank=True, related_name='formularios')
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    tipo_respostas = models.CharField(
        max_length=10,
        choices=TIPO_RESPOSTAS_CHOICES,
        default='todas',
    )

    def __str__(self):
        return self.nome


class Avaliador(models.Model):
    nome = models.CharField(max_length=100)
    email = models.EmailField()
    profissao = models.CharField(max_length=100, blank=True, null=True)
    formulario = models.ForeignKey(Formulario, on_delete=models.CASCADE, related_name='avaliadores')
    data_resposta = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("email", "formulario"),
                name="unique_evaluator_email_per_form",
            ),
        ]

    def __str__(self):
        return f"{self.nome} - {self.email}"


class AvaliacaoFormulario(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    avaliador = models.ForeignKey(Avaliador, on_delete=models.CASCADE, null=True, blank=True, related_name='avaliacoes')

    resposta = models.ForeignKey(Resposta, on_delete=models.CASCADE)
    metrica = models.ForeignKey(Metrica, on_delete=models.SET_NULL, null=True)
    avaliacao_quali = models.TextField(blank=True, null=True)
    avaliacao_quanti = models.IntegerField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["usuario", "metrica"],
                condition=models.Q(avaliacao_quanti__isnull=False),
                name="aval_form_user_metric_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(avaliacao_quanti__isnull=True)
                    | models.Q(avaliacao_quanti__gte=1, avaliacao_quanti__lte=5)
                ),
                name="form_score_between_1_and_5",
            ),
            models.UniqueConstraint(
                fields=("avaliador", "resposta", "metrica"),
                condition=(
                    models.Q(avaliador__isnull=False)
                    & models.Q(metrica__isnull=False)
                ),
                name="unique_form_score_per_evaluator",
            ),
        ]

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
        indexes = [
            models.Index(
                fields=["usuario", "metrica"],
                condition=models.Q(
                    erro=False,
                    avaliacao_quanti__isnull=False,
                ),
                name="aval_judge_user_metric_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(avaliacao_quanti__isnull=True)
                    | models.Q(avaliacao_quanti__gte=1, avaliacao_quanti__lte=5)
                ),
                name="judge_score_between_1_and_5",
            ),
        ]

    def __str__(self):
        nome_juiz = self.juiz.nome if self.juiz else 'Juiz removido'
        return f"{nome_juiz} avaliou {self.resposta}"


class AvaliacaoPublicaLLM(models.Model):
    """Avaliação cruzada entre LLMs públicas para respostas do chat público."""
    juiz = models.ForeignKey(
        LLMPublica,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="avaliacoes_publicas_como_juiz",
    )
    resposta = models.ForeignKey(
        RespostaPublica,
        on_delete=models.CASCADE,
        related_name="avaliacoes_cruzadas",
    )
    metrica = models.ForeignKey(Metrica, on_delete=models.SET_NULL, null=True)
    avaliacao_quali = models.TextField(blank=True, null=True)
    avaliacao_quanti = models.IntegerField(blank=True, null=True)
    justificativa_geral = models.TextField(blank=True, null=True)
    erro = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Avaliação Pública por LLM"
        verbose_name_plural = "Avaliações Públicas por LLM"
        ordering = ["-atualizado_em"]
        unique_together = ("juiz", "resposta", "metrica")
        indexes = [
            models.Index(
                fields=["-atualizado_em"],
                condition=models.Q(
                    erro=False,
                    avaliacao_quanti__isnull=False,
                ),
                name="aval_pub_updated_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(avaliacao_quanti__isnull=True)
                    | models.Q(avaliacao_quanti__gte=1, avaliacao_quanti__lte=5)
                ),
                name="public_judge_score_between_1_and_5",
            ),
        ]

    def __str__(self):
        nome_juiz = self.juiz.nome if self.juiz else "Juiz removido"
        return f"{nome_juiz} avaliou {self.resposta}"
