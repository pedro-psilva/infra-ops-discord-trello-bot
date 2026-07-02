# Imagem para rodar o Infra Ops como worker sempre ligado (listener de DM/mencoes).
# Funciona em qualquer host de container: Fly.io, Koyeb, Railway, Render (worker), VM propria.
FROM python:3.12-slim

WORKDIR /app

# Dependencias primeiro (melhor cache de build)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Sem porta exposta: e um worker que mantem conexao com o Gateway do Discord.
# As variaveis de ambiente (DISCORD_BOT_TOKEN, TRELLO_*, DISCORD_DM_ALLOWED_USER_IDS, etc.)
# devem ser configuradas no painel do host.
CMD ["python", "main.py", "--listen"]
