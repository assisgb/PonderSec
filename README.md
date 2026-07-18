# 🔐 PonderSEC

Plataforma para **avaliação de Large Language Models (LLMs) em tarefas de cibersegurança**.

O objetivo do projeto é permitir a **execução, análise e comparação de respostas de modelos de linguagem** quando submetidos a **prompts relacionados a segurança da informação**, auxiliando pesquisas na área de **IA aplicada à cibersegurança**.

Este projeto foi desenvolvido no contexto de **Iniciação Científica (PIBITI/CNPq)** na **Universidade Federal do Amazonas (UFAM)**.

---

# 🎯 Objetivo

O **PonderSEC** busca fornecer um ambiente para:

* Testar **LLMs em cenários de segurança**
* Avaliar **respostas geradas pelos modelos**
* Organizar **datasets de prompts de cibersegurança**
* Facilitar **análises experimentais em pesquisas acadêmicas**

---

# 🏗️ Arquitetura

O sistema utiliza uma arquitetura baseada em containers para facilitar a execução e reprodução do ambiente.

Principais tecnologias utilizadas:

* **Python**
* **Django**
* **PostgreSQL**
* **Docker**
* **Docker Compose**

---

# ⚙️ Requisitos

Antes de executar o projeto, certifique-se de possuir instalado:

* Docker
* Docker Compose

---

# 🚀 Instruções de Uso

## 1️⃣ Gerar a imagem Docker

```bash
sudo docker compose build
```

---

## 2️⃣ Rodar os contêineres

```bash
sudo docker compose up
```

O sistema iniciará automaticamente os serviços definidos no `docker-compose.yml`.

---

# ✅ Validação e testes

O JudgeAI usa exclusivamente **Completude, Acurácia, Diretividade e Clareza**, com notas inteiras de **1 a 5**.

Depois de atualizar o código, aplique as migrações e execute os testes:

```bash
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py test
```

Os limites das chamadas externas e a concorrência do servidor podem ser
configurados no `.env`:

```dotenv
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=gere-uma-chave-longa-e-unica
LLM_REQUEST_TIMEOUT_SECONDS=45
LLM_STREAM_TIMEOUT_SECONDS=60
LLM_MODELS_MAX_WORKERS=4
LLM_EVALUATION_MAX_WORKERS=4
LLM_CLIENT_CACHE_TTL_SECONDS=300
LLM_CLIENT_CACHE_MAX_SIZE=8
LLM_TRANSIENT_MAX_ATTEMPTS=2
LLM_TRANSIENT_RETRY_DELAY_SECONDS=0.2
PUBLIC_CHAT_RATE_LIMIT=30
PUBLIC_EVALUATION_RATE_LIMIT=120
PUBLIC_CHAT_RATE_WINDOW_SECONDS=60
PUBLIC_RATE_TRUST_X_REAL_IP=false
QUESTION_UPLOAD_MAX_BYTES=10485760
QUESTION_UPLOAD_MAX_ITEMS=20000
DB_CONN_MAX_AGE=60
DB_CONN_HEALTH_CHECKS=true
GUNICORN_WORKERS=3
GUNICORN_THREADS=4
GUNICORN_TIMEOUT_SECONDS=180
LOG_LEVEL=INFO
```

Em produção, `DJANGO_SECRET_KEY` é obrigatória com `DJANGO_DEBUG=False`; ela
protege sessões e os tokens assinados usados pela avaliação assíncrona.

O Gunicorn usa workers `gthread`, apropriados para as chamadas de LLM que passam
boa parte do tempo aguardando I/O. A capacidade máxima de requisições simultâneas
por instância é aproximadamente `GUNICORN_WORKERS * GUNICORN_THREADS`; ajuste esses
valores conforme a CPU, a memória e os limites dos provedores de LLM.

`LLM_MODELS_MAX_WORKERS` limita quantos modelos diferentes respondem à mesma
pergunta ao mesmo tempo. `LLM_EVALUATION_MAX_WORKERS` reserva um pool separado
para o JudgeAI, impedindo que avaliações atrasem novas respostas. Os pools são
reutilizados por processo; o processamento em lote envia uma pergunta por vez,
evitando rajadas repetidas nas mesmas chaves de Groq/Gemini.

Os clientes HTTP dos provedores são reutilizados por thread por até 300 segundos,
evitando novos handshakes DNS/TLS. Falhas transitórias de conexão ou 5xx recebem
no máximo uma repetição curta; autenticação, cota e modelo inexistente falham sem
repetir a cobrança.

No PostgreSQL, `DB_CONN_MAX_AGE` reaproveita conexões por thread e
`DB_CONN_HEALTH_CHECKS` valida conexões persistentes antes do uso. O contêiner
aplica somente migrações versionadas na inicialização; ele não gera migrações em
produção.

O chat público aceita, por padrão, 30 gerações por minuto por origem em cada
processo. Em instalações com vários servidores, configure um cache compartilhado
para que o limite seja global. Ative `PUBLIC_RATE_TRUST_X_REAL_IP` somente quando
um proxy confiável, como o Nginx fornecido, sobrescrever `X-Real-IP`.

`GUNICORN_TIMEOUT_SECONDS` deve ser sempre maior que
`LLM_STREAM_TIMEOUT_SECONDS`. Os defaults do Compose usam, respectivamente, 180 e
60 segundos. O proxy Nginx desativa o buffering das respostas e usa timeouts de
300 segundos; se o timeout do Gunicorn for elevado acima desse valor, atualize
também `proxy_send_timeout`, `proxy_read_timeout` e `send_timeout` em
`nginx/nginx.conf`.

As chaves de API são relidas do banco a cada chamada. Depois de substituir uma chave no Setup ou no painel público, não é necessário reiniciar o processo para invalidar cache de cliente.

---

# 📂 Estrutura do Projeto

```
pondersec/
│
├── docker-compose.yml
├── Dockerfile
├── app/
│   ├── models
│   ├── views
│   ├── services
│   └── prompts
│
├── database/
└── README.md
```

---

# 🔬 Contexto Acadêmico

Este projeto está sendo desenvolvido como parte de uma **pesquisa em Inteligência Artificial aplicada à Cibersegurança**, no programa de **Iniciação Científica PIBITI/CNPq**.

Instituição: **Universidade Federal do Amazonas (UFAM)**

---

# 👥 Autores

* **Gabriel Assis**
* **Miguel Moraes**
* **Luiz Barbosa**

---
