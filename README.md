# PonderSEC

Plataforma web para execução, avaliação e comparação de modelos de linguagem em tarefas de cibersegurança. O projeto foi desenvolvido no contexto de Iniciação Científica PIBITI/CNPq na Universidade Federal do Amazonas (UFAM).

## Funcionalidades

- chat público com múltiplas LLMs;
- cadastro de questões e categorias de segurança;
- comparação de respostas entre modelos;
- avaliações humanas quantitativas e qualitativas;
- avaliação cruzada com LLMs atuando como juízes;
- dashboards para análise dos resultados;
- painel administrativo separado para o chat público.

## Tecnologias

- Python 3.11 e Django;
- PostgreSQL;
- Gunicorn e WhiteNoise;
- Docker e Docker Compose.

## Estrutura

```text
PonderSec/
├── pondersec/            # Configurações centrais do Django
├── responsegenerator/    # Chat, questões, avaliações e dashboards
├── usuarios/             # Cadastro, autenticação e arquivos estáticos
├── templates/partials/   # Componentes compartilhados
├── locale/               # Traduções
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Execução local

1. Crie o arquivo de ambiente:

   ```bash
   cp .env.exemplo .env
   ```

2. Substitua as credenciais de exemplo e gere uma chave Django:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(64))"
   ```

3. Construa e inicie os serviços:

   ```bash
   docker compose up --build
   ```

4. Acesse:

   - aplicação: <http://localhost:8000>;
   - pgAdmin: <http://localhost:5050>;
   - painel PonderSEC: <http://localhost:8000/admin-pondersec/>.

O administrador do painel é criado de forma idempotente pelas variáveis `ADMIN_PONDERSEC_*`. Também é possível gerenciá-lo manualmente:

```bash
docker compose exec web python manage.py criar_admin --email admin@example.com
```

## Validação

```bash
docker compose exec web python manage.py check
docker compose exec web python manage.py test
```

Antes de criar migrations, revise os modelos e execute explicitamente:

```bash
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate
```

## Publicação

- defina `DEBUG=False`;
- use uma `SECRET_KEY` longa e exclusiva;
- limite `ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` ao domínio publicado;
- sirva a aplicação atrás de HTTPS;
- não versione `.env`, bancos locais, certificados ou chaves de API;
- rotacione qualquer credencial que já tenha aparecido no histórico Git.

## Autores

- Gabriel Assis
- Miguel Moraes
- Luiz Barbosa
