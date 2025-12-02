## Instruções rápidas para agentes de código (Projeto PonderSec)

Objetivo curto:
- Aplicação Django simples chamada `pondersec` com um único app `usuarios`.
- Banco: SQLite em `db.sqlite3` (arquivo no repositório).

Contexto arquitetural (o essencial que ajuda a ser produtivo):
- Projeto Django padrão: settings em `pondersec/settings.py`, WSGI/ASGI em `pondersec/`.
- App principal: `usuarios/` contém `views.py`, `urls.py`, `models.py` (models vazio) e templates em `usuarios/templates/`.
- Rotas: `pondersec/urls.py` faz `include('usuarios.urls')`. As rotas expostas hoje são `''` → login e `cadastro/` → criação de usuário (ver `usuarios/urls.py`).

Padrões e convenções específicas do repositório:
- Templates são fornecidos no app (`usuarios/templates/`) com nomes: `login.html`, `cadastro.html`, `tela_inicial.html`.
- Autenticação usa o `django.contrib.auth.User` direto em `usuarios/views.py`. Criação de usuário usa `User.objects.create_user(...)`.
- Fluxo de login em `usuarios/views.py`: usa `authenticate()` e, se válido, chama `tela_inicial(request)` (nota: não faz redirect HTTP, chama a view diretamente).

Comandos úteis / fluxo de desenvolvimento:
- Rodar localmente: usar o utilitário Django padrão
  - `python manage.py runserver`
- Docker (já documentado no `README.md`):
  - `docker build -t "pondersec" .`
  - `docker run -p 8000:8000 pondersec`
- Banco de dados: arquivo SQLite `db.sqlite3` no repositório — nenhuma configuração extra necessária por padrão.

Arquivos-chave a checar ao implementar mudanças:
- `usuarios/views.py` — lógica de negócio de login/cadastro (ponto principal para alterações de autenticação).
- `usuarios/urls.py` — onde adicionar novas rotas do app.
- `usuarios/templates/` — front-end (HTML) usado pelas views.
- `pondersec/settings.py` — configurações globais (DEBUG=True atualmente, SECRET_KEY presente no repositório).

Observações práticas encontradas (úteis para o agente):
- Não há testes automatizados no repositório (`usuarios/tests.py` existe mas vazio). Evite pressupor suites de testes já configuradas.
- `requirements.txt` contém apenas `django`. Antes de rodar, instale dependências em ambiente venv.
- Segurança/ops: `DEBUG = True` e `SECRET_KEY` em texto plano — mudanças em produção exigem mover essas variáveis para env.

Como editar/estender rapidamente (exemplos):
- Para adicionar página protegida: criar view em `usuarios/views.py`, adicionar rota em `usuarios/urls.py` e template em `usuarios/templates/`.
- Para criar usuário com validação extra: modificar `cadastro` em `usuarios/views.py` (atualmente valida apenas igualdade de senha e existência de username).

Critérios de sucesso para mudanças de PRs pequenas:
- Mantém comportamento atual em rotas existentes (`/` e `/cadastro/`).
- Não introduz dependências externas sem atualizar `requirements.txt`.
- Atualiza `README.md` se comandos de execução forem alterados.

Se algo estiver faltando no arquivo de instruções ou quiseres que eu detalhe exemplos de PR/patch, diga quais áreas priorizar (rotas, autenticação ou Docker).
