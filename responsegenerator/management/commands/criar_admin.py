"""Cria (ou atualiza a senha de) um AdminPonderSec.

Uso:
    python manage.py criar_admin --email admin@pondersec --senha trocar123 --nome "Admin"
    python manage.py criar_admin --email admin@pondersec --senha nova_senha  (atualiza senha)
"""

import getpass

from django.core.management.base import BaseCommand, CommandError

from responsegenerator.models import AdminPonderSec


class Command(BaseCommand):
    help = "Cria um admin do painel /admin-pondersec/ ou atualiza a senha de um existente."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="E-mail do admin (chave única).")
        parser.add_argument("--senha", required=False, help="Senha. Se omitido, será pedida interativamente.")
        parser.add_argument("--nome", required=False, default=None, help="Nome de exibição. Default: parte local do e-mail.")

    def handle(self, *args, **options):
        email = (options["email"] or "").strip().lower()
        senha = options.get("senha")
        nome = options.get("nome") or email.split("@")[0]

        if not email:
            raise CommandError("E-mail é obrigatório.")

        if not senha:
            senha = getpass.getpass("Senha: ")
            confirma = getpass.getpass("Confirmar senha: ")
            if senha != confirma:
                raise CommandError("Senhas não conferem.")

        if len(senha) < 8:
            raise CommandError("Senha precisa ter ao menos 8 caracteres.")

        admin, criado = AdminPonderSec.objects.get_or_create(
            email=email,
            defaults={"nome": nome},
        )
        admin.set_senha(senha)
        if not criado and nome:
            admin.nome = nome
        admin.ativo = True
        admin.save()

        verbo = "criado" if criado else "atualizado"
        self.stdout.write(self.style.SUCCESS(f"Admin {verbo}: {admin.nome} <{admin.email}>"))
