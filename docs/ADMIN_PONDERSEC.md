# Painel Admin (`/admin-pondersec/`)

Este documento descreve como criar e gerenciar o **admin** do PonderSEC — pessoa responsável por cadastrar as **API keys das LLMs usadas no chat público** da página inicial.

> **Por que existe um painel separado?** As LLMs dos pesquisadores (cadastradas em `/setup_llm/`) são privadas — cada pesquisador paga com a sua própria API key. O chat público (acessível em `/`, sem login) **não pode** usar essas keys. O painel admin é o único lugar onde se cadastra uma API key que o público vai consumir.

---

## 1. Criar o primeiro admin (caminho recomendado — automático via `.env`)

1. Abra o arquivo `.env` na raiz do projeto. Se ele ainda não existir, copie de `.env.exemplo`:

   ```bash
   cp .env.exemplo .env
   ```

2. Edite as três variáveis do bloco do admin:

   ```dotenv
   ADMIN_PONDERSEC_EMAIL="voce@exemplo.com"
   ADMIN_PONDERSEC_NOME="Seu Nome"
   ADMIN_PONDERSEC_SENHA="uma-senha-com-pelo-menos-8-chars"
   ```

3. Suba o stack normalmente:

   ```bash
   sudo docker compose up
   ```

   No log do container `web` você vai ver uma linha tipo:

   ```
   [bootstrap_admin] Admin criado: Seu Nome <voce@exemplo.com>
   ```

4. Acesse `http://localhost:8000/admin-pondersec/login/` e entre com o e-mail e a senha definidos.

### Observações importantes

- **Idempotente:** o bootstrap só cria o admin se ainda não existir um com aquele e-mail. Pode subir o stack quantas vezes quiser que nada é sobrescrito. Se as variáveis estiverem vazias, o bootstrap simplesmente é pulado.
- **Não troca senha automaticamente:** se você editar `ADMIN_PONDERSEC_SENHA` no `.env` depois que o admin já existe, a senha **não** é trocada. Veja a seção "Trocar senha" abaixo.
- **Depois do primeiro login você pode esvaziar as variáveis no `.env`** — o admin persiste no banco. Manter as variáveis preenchidas é opcional e ajuda em reinstalações.

---

## 2. Criar/atualizar admin manualmente (caminho alternativo via CLI)

Útil quando você não quer mexer no `.env`, quer trocar a senha, ou criar um segundo admin.

```bash
# cria (ou atualiza a senha de) um admin
sudo docker compose exec -it web python manage.py criar_admin \
    --email voce@exemplo.com \
    --nome "Seu Nome"
# vai pedir a senha duas vezes pra confirmar
```

Se preferir passar a senha na linha de comando (cuidado: fica no histórico do shell):

```bash
sudo docker compose exec web python manage.py criar_admin \
    --email voce@exemplo.com \
    --senha minha-senha-aqui \
    --nome "Seu Nome"
```

Diferenças do `bootstrap_admin`:

| | `bootstrap_admin` | `criar_admin` |
|---|---|---|
| Trigger | Automático em todo `docker compose up` | Manual |
| Origem das credenciais | Variáveis de ambiente | Flags do comando |
| Se o e-mail já existe | **Não faz nada** | **Atualiza a senha** |
| Útil para | Primeiro acesso | Trocar senha, criar outro admin |

---

## 3. Fazer login

1. `http://localhost:8000/admin-pondersec/login/`
2. E-mail + senha cadastrados.
3. Após login você cai em `/admin-pondersec/` (home com os stats e atalho pra LLMs Públicas).

A sessão do admin é independente da sessão de pesquisador — você pode estar logado nos dois ao mesmo tempo sem conflito.

---

## 4. Cadastrar LLMs Públicas

Em `/admin-pondersec/llms-publicas/`:

1. Clique em **Adicionar LLM**.
2. Preencha:
   - **Nome do modelo** — ex: `gpt-4o-mini`, `gemini-2.5-flash`, `llama-3.1-70b-versatile`. É o que vai pro `model` do SDK do provedor.
   - **Provedor** — OpenAI / Gemini / Groq / DeepSeek. Determina qual SDK é usado pra fazer a chamada (o dispatcher faz match por substring desta string com `nome` + `descricao`).
   - **API Key** — a chave que será usada. Esta chave **vai cobrar a sua conta** sempre que alguém usar o chat público; trate-a com a mesma seriedade de uma chave de produção.
3. Salvar. A LLM já fica ativa e disponível pro chat público.

Cada visitante anônimo dispara **todas** as LLMs ativas em paralelo (até 3 simultâneas via `ThreadPoolExecutor`). Para desativar uma LLM sem deletar, clique no toggle do card.

---

## 5. Trocar a senha do admin

```bash
sudo docker compose exec -it web python manage.py criar_admin \
    --email voce@exemplo.com
# pede a senha nova duas vezes
```

Como o e-mail já existe, o comando só atualiza a senha (não cria duplicata).

---

## 6. Bloquear/desativar um admin

Não tem UI ainda. Via shell do Django:

```bash
sudo docker compose exec -it web python manage.py shell
```

```python
from responsegenerator.models import AdminPonderSec
a = AdminPonderSec.objects.get(email="voce@exemplo.com")
a.ativo = False
a.save()
```

Um admin com `ativo=False` não consegue logar. Para reativar, é só inverter pra `True`.

---

## 7. Esquecer/resetar admin completamente

```bash
sudo docker compose exec -it web python manage.py shell
```

```python
from responsegenerator.models import AdminPonderSec
AdminPonderSec.objects.all().delete()
```

Depois disso, o próximo `docker compose up` vai recriar o admin a partir do `.env` (se as variáveis estiverem preenchidas).

---

## 8. Troubleshooting

**"E-mail ou senha inválidos" no primeiro acesso**
- O bootstrap pode não ter rodado. Veja os logs do container `web` (`sudo docker compose logs web | grep bootstrap_admin`).
- A senha precisa ter ≥ 8 caracteres — se for menor, o bootstrap aborta com `sys.exit(1)`.
- A tela de login mostra um aviso roxo/azul quando **nenhum admin** existe no banco — se você vê esse aviso, é sinal de que o bootstrap não rodou ou as variáveis estavam vazias.

**Quero usar outra senha mas o bootstrap não trocou**
- Comportamento esperado: o bootstrap é idempotente. Use `criar_admin` (seção 5).

**Não consigo achar o `.env`**
- Ele é gitignored. Se nunca existiu, copie do exemplo: `cp .env.exemplo .env`.
