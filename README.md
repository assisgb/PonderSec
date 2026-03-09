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

# 👨‍💻 Contato

GitHub:
https://github.com/miguelmoraesx

LinkedIn:
https://www.linkedin.com/in/miguel-moraes-7a2535309/
