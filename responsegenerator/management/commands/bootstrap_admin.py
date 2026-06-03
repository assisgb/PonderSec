"""Cria o admin do painel /admin-pondersec/ a partir de variáveis de ambiente.

Pensado para rodar a cada `docker compose up` — é idempotente:
- se o admin do e-mail informado já existir, não faz nada
- se NENHUM admin existir e as variáveis estiverem setadas, cria

Variáveis lidas:
    ADMIN_PONDERSEC_EMAIL  (obrigatória)
    ADMIN_PONDERSEC_SENHA  (obrigatória, mínimo 8 caracteres)
    ADMIN_PONDERSEC_NOME   (opcional, default = parte local do e-mail)

Se as variáveis não estiverem setadas, o comando termina silenciosamente sem erro
(para não quebrar `docker compose up` em ambientes que não usam o painel).
"""

import os
import sys

from django.core.management.base import BaseCommand

from responsegenerator.models import AdminPonderSec


class Command(BaseCommand):
    help = "Bootstrap idempotente do admin do painel via variáveis de ambiente."

    def handle(self, *args, **options):
        email = (os.environ.get("ADMIN_PONDERSEC_EMAIL") or "").strip().lower()
        senha = os.environ.get("ADMIN_PONDERSEC_SENHA") or ""
        nome = (os.environ.get("ADMIN_PONDERSEC_NOME") or "").strip()

        if not email or not senha:
            self.stdout.write("[bootstrap_admin] ADMIN_PONDERSEC_EMAIL/SENHA não setados — pulando.")
            return

        if len(senha) < 8:
            self.stderr.write("[bootstrap_admin] ADMIN_PONDERSEC_SENHA tem menos de 8 caracteres — abortando.")
            sys.exit(1)

        if not nome:
            nome = email.split("@")[0]

        if AdminPonderSec.objects.filter(email=email).exists():
            self.stdout.write(f"[bootstrap_admin] Admin {email} já existe — nada a fazer.")
            return

        admin = AdminPonderSec(email=email, nome=nome, ativo=True)
        admin.set_senha(senha)
        admin.save()
        self.stdout.write(self.style.SUCCESS(
            f"[bootstrap_admin] Admin criado: {admin.nome} <{admin.email}>"
        ))
