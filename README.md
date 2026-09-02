# PonderSEC

Plataforma web para **avaliação de Large Language Models (LLMs) em tarefas de cibersegurança**, desenvolvida como pesquisa de Iniciação Científica (PIBITI/CNPq) na Universidade Federal do Amazonas (UFAM).

O sistema permite que pesquisadores submetam perguntas a múltiplos modelos de IA, coletem avaliações de especialistas humanos, e executem avaliação automatizada com o **JudgeAI** — um pipeline que usa LLMs como juízes para pontuar as respostas geradas.

---

## Funcionalidades

### Chat Público (`/`)
- Interface acessível sem login para qualquer visitante
- Envia perguntas simultaneamente para todas as LLMs públicas ativas (até 3 em paralelo)
- Avaliação cruzada automática: cada LLM pública avalia as respostas das outras (JudgeAI público)
- Rate limiting: 30 gerações/minuto por IP por padrão

### Área do Pesquisador (login obrigatório)
| Rota | Descrição |
|------|-----------|
| `/menu/` | Dashboard principal |
| `/questoes/` | Gerenciar banco de perguntas |
| `/upload-perguntas/` | Upload em lote (JSON/TXT, até 20.000 itens) |
| `/setup_llm/` | Cadastrar LLMs privadas (API keys do pesquisador) |
| `/setup_avaliacao/` | Configurar métricas de avaliação |
| `/gerar_resposta/` | Disparar geração de respostas para questões |
| `/avaliacao/` | Avaliar respostas manualmente |
| `/avaliacao/dashboard/` | Dashboard com scores consolidados |
| `/avaliacao/dashboard-comparativo/` | Comparativo entre LLMs |
| `/avaliacao/exportar/` | Exportar avaliações em CSV |
| `/menu_avaliacao/` | Menu do módulo de avaliação |
| `/juizes/comparador/` | Comparar avaliações do JudgeAI |
| `/juizes/avaliar/` | Executar JudgeAI (LLM-as-judge) |
| `/formularios/` | Criar formulários para especialistas externos |
| `/avaliacao/responder/<id>/` | Link público para especialistas responderem |

### Painel Admin (`/admin-pondersec/`)
- Auth separada dos pesquisadores
- Gerenciar LLMs públicas (nome do modelo, provedor, API key)
- Gerenciar métricas públicas usadas na avaliação cruzada do chat
- Ver avaliações do chat público

---

## Arquitetura

```
PonderSEC/
├── pondersec/              # Configurações Django (settings, urls, wsgi)
├── responsegenerator/      # App principal
│   ├── models.py           # Modelos: LLM, Questao, Resposta, Avaliacao, Formulario, JudgeAI...
│   ├── views.py            # Views (pesquisador + admin + chat público)
│   ├── llm_client.py       # Client unificado para Gemini / Groq / OpenAI / DeepSeek
│   ├── judgeai_metrics.py  # Pipeline de avaliação automática (JudgeAI)
│   ├── executors.py        # ThreadPoolExecutors para chamadas paralelas
│   └── templates/          # Templates HTML
├── usuarios/               # App de auth dos pesquisadores
├── nginx/                  # Config nginx (produção)
├── docs/                   # ADMIN_PONDERSEC.md e outros
├── exemplos/               # Arquivos de exemplo para upload
├── docker-compose.yml      # Stack de produção
├── docker-compose.local.yml # Stack de desenvolvimento local
├── Dockerfile
├── requirements.txt
└── .env.exemplo
```

**Provedores de LLM suportados:** Groq · Google Gemini · OpenAI · DeepSeek

O dispatcher identifica o provedor pelo campo `descricao` ou `nome` do modelo (match por substring: `gemini`/`google`, `groq`/`llama`/`mixtral`, `openai`/`gpt`, `deepseek`).

---

## Pré-requisitos

- [Docker](https://docs.docker.com/get-docker/) 24+
- [Docker Compose](https://docs.docker.com/compose/install/) v2+

---

## Rodando localmente

### 1. Clone e configure o ambiente

```bash
git clone <url-do-repo>
cd PonderSec
cp .env.exemplo .env
```

### 2. Edite o `.env`

Abra o `.env` e ajuste as variáveis mínimas para rodar localmente:

```dotenv
# Banco de dados
POSTGRES_DB=pondersec
POSTGRES_USER=pondersec
POSTGRES_PASSWORD=pondersec123
POSTGRES_PORT=5432

DB_ENGINE=django.db.backends.postgresql
DB_NAME=pondersec
DB_USER=pondersec
DB_PASSWORD=pondersec123
DB_HOST=db
DB_PORT=5432

# Django — pode usar qualquer chave para dev
DJANGO_DEBUG=True
DJANGO_SECRET_KEY=dev-secret-key-troque-em-producao

# Admin do painel /admin-pondersec/
ADMIN_PONDERSEC_EMAIL=admin@pondersec
ADMIN_PONDERSEC_NOME=Admin
ADMIN_PONDERSEC_SENHA=admin1234

# pgAdmin (opcional)
PGADMIN_PORT=5050
PGADMIN_EMAIL=admin@admin.com
PGADMIN_PASSWORD=admin
```

> Para e-mail em dev, não preencha `HOST_API_EMAIL`/`SENHA_API_EMAIL` — o sistema usa o backend de console automaticamente quando `DJANGO_DEBUG=True`.

### 3. Suba o stack local

```bash
docker compose -f docker-compose.local.yml up --build
```

O container executa automaticamente:
- `migrate` — aplica todas as migrações
- `bootstrap_admin` — cria o admin do painel `/admin-pondersec/` (se as vars estiverem no `.env`)
- `collectstatic` — coleta arquivos estáticos
- Django `runserver` com hot-reload ativo

### 4. Acesse

| URL | Descrição |
|-----|-----------|
| `http://localhost:8000/` | Chat público |
| `http://localhost:8000/auth/` | Login pesquisador |
| `http://localhost:8000/auth/cadastro/` | Cadastro de pesquisador |
| `http://localhost:8000/admin-pondersec/login/` | Login admin |
| `http://localhost:8000/admin/` | Django admin (superuser) |
| `http://localhost:5050/` | pgAdmin (opcional) |

### 5. Criar um superuser Django (opcional)

```bash
docker compose -f docker-compose.local.yml exec web python manage.py createsuperuser
```

### 6. Rodar os testes

```bash
docker compose -f docker-compose.local.yml exec web python manage.py test
```

---

## Rodando em produção

### 1. Configure o `.env` com valores reais

```dotenv
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<chave-longa-e-aleatoria>   # obrigatória em produção
POSTGRES_PASSWORD=<senha-forte>
# ... demais variáveis
```

Gere uma `SECRET_KEY` segura:
```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

### 2. Suba o stack de produção

```bash
sudo docker compose up -d --build
```

O stack de produção usa **Gunicorn + gthread** (adequado para chamadas I/O-bound às APIs de LLM). O Nginx está disponível em `nginx/nginx.conf` mas comentado no compose — habilite quando necessário para SSL/proxy.

---

## Configurando LLMs

### LLMs públicas (chat público — painel admin)

1. Acesse `http://localhost:8000/admin-pondersec/login/`
2. Login com as credenciais do `ADMIN_PONDERSEC_*` do `.env`
3. Vá em **LLMs Públicas** → **Adicionar LLM**
4. Preencha:
   - **Nome do modelo:** identificador exato da API, ex: `gemini-3.5-flash`, `llama-3.3-70b-versatile`, `gpt-4o-mini`
   - **Provedor (descrição):** `Gemini`, `Groq`, `OpenAI` ou `DeepSeek` — usado pelo dispatcher para selecionar o SDK
   - **API Key:** a chave do provedor

> A API key é relida do banco a cada chamada. Trocar a chave no painel tem efeito imediato, sem reiniciar o serviço.

### LLMs privadas (pesquisador)

Após login de pesquisador, acesse `/setup_llm/` e cadastre suas LLMs. Cada pesquisador tem suas próprias chaves — não compartilhadas com o chat público.

---

## Variáveis de ambiente — referência completa

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `DJANGO_DEBUG` | `False` | Modo debug — nunca `True` em produção |
| `DJANGO_SECRET_KEY` | — | Obrigatória em produção |
| `POSTGRES_DB` | — | Nome do banco |
| `POSTGRES_USER` | — | Usuário do banco |
| `POSTGRES_PASSWORD` | — | Senha do banco |
| `DB_HOST` | `db` | Host do banco (nome do serviço Docker) |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `45` | Timeout para chamadas síncronas às LLMs |
| `LLM_STREAM_TIMEOUT_SECONDS` | `60` | Timeout para streaming |
| `LLM_MODELS_MAX_WORKERS` | `4` | Paralelismo de modelos por pergunta |
| `LLM_EVALUATION_MAX_WORKERS` | `4` | Paralelismo do JudgeAI |
| `LLM_CLIENT_CACHE_TTL_SECONDS` | `300` | TTL do cache de clientes HTTP |
| `LLM_TRANSIENT_MAX_ATTEMPTS` | `2` | Retentativas em erros transitórios |
| `PUBLIC_CHAT_RATE_LIMIT` | `30` | Gerações/minuto por IP no chat público |
| `PUBLIC_EVALUATION_RATE_LIMIT` | `120` | Avaliações/minuto no chat público |
| `PUBLIC_RATE_TRUST_X_REAL_IP` | `false` | Confiar no `X-Real-IP` (ativar só atrás de proxy confiável) |
| `QUESTION_UPLOAD_MAX_BYTES` | `10485760` | Tamanho máximo de upload (10 MB) |
| `QUESTION_UPLOAD_MAX_ITEMS` | `20000` | Máximo de perguntas por upload |
| `GUNICORN_WORKERS` | `3` | Workers do Gunicorn |
| `GUNICORN_THREADS` | `4` | Threads por worker |
| `GUNICORN_TIMEOUT_SECONDS` | `180` | Timeout do worker (deve ser > `LLM_STREAM_TIMEOUT_SECONDS`) |
| `ADMIN_PONDERSEC_EMAIL` | — | Email do admin criado no bootstrap |
| `ADMIN_PONDERSEC_NOME` | — | Nome do admin criado no bootstrap |
| `ADMIN_PONDERSEC_SENHA` | — | Senha (≥ 8 chars) do admin criado no bootstrap |
| `HOST_API_EMAIL` | — | Login SMTP (Brevo) para envio de e-mails |
| `SENHA_API_EMAIL` | — | Chave SMTP (Brevo) |

---

## JudgeAI

O JudgeAI usa LLMs como juízes para avaliar automaticamente as respostas. As métricas padrão são:

| Métrica | Escala | Descrição |
|---------|--------|-----------|
| Completude | 1–5 | A resposta cobre todos os pontos da pergunta? |
| Acurácia | 1–5 | A informação é tecnicamente correta? |
| Diretividade | 1–5 | A resposta vai direto ao ponto? |
| Clareza | 1–5 | A resposta é fácil de entender? |

Para executar o JudgeAI em pesquisa: `/juizes/avaliar/`  
Para ver comparativo: `/juizes/comparador/`

---

## Formulários para especialistas

O módulo de formulários permite enviar links para especialistas externos avaliarem respostas sem precisar de login:

1. Crie um formulário em `/avaliacao/adicionar/`
2. Selecione questões e configure quais respostas avaliar
3. Compartilhe o link `/avaliacao/responder/<id>/` com os especialistas

---

## Exportação de dados

- **CSV de avaliações:** `/avaliacao/exportar/` (pesquisador)
- **Templates de upload:** `/download-template-perguntas/json/` ou `/download-template-perguntas/txt/`

---

## Gerenciamento do Admin PonderSec

Veja `docs/ADMIN_PONDERSEC.md` para instruções detalhadas sobre:
- Criar/trocar senha do admin via CLI
- Desativar/reativar admins
- Reset completo do admin

---

## Contexto Acadêmico

Pesquisa em Inteligência Artificial aplicada à Cibersegurança — **PIBITI/CNPq**  
Instituição: **Universidade Federal do Amazonas (UFAM)**

**Autores:** Gabriel Assis · Miguel Moraes · Luiz Barbosa
