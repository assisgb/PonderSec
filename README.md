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

Os tempos máximos das chamadas externas podem ser configurados no `.env`:

```dotenv
LLM_REQUEST_TIMEOUT_SECONDS=45
LLM_STREAM_TIMEOUT_SECONDS=60
LOG_LEVEL=INFO
```

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

