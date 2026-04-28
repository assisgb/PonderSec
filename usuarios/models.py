from django.db import models
from django.contrib.auth.models import User

class CodigoVerificacao(models.Model):
    usuario = models.OneToOneField(User, on_delete=models.CASCADE)
    codigo = models.CharField(max_length=6)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Código de {self.usuario.username}"    

