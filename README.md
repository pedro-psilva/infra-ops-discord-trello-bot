# Bot Discord -> Trello

Bot em Python para ler mensagens de um ou mais canais do Discord, identificar tarefas de onboarding/offboarding, criar cards reais no Trello a partir de templates, atender pedidos por mencao ao bot e confirmar o processamento com `:white_check_mark:` na mensagem.

## Arquitetura escolhida

Para ficar o mais perto possivel de custo zero, o projeto foi estruturado para rodar como um job agendado, sem manter uma conexao 24/7 com o Gateway do Discord. A execucao diaria pode ficar no GitHub Actions, que segundo a documentacao oficial aceita agendamento com `schedule`, suporta timezone IANA e roda no ultimo commit da branch padrao.

## Como funciona

1. O workflow ou a execucao local chama `python main.py`.
2. O script lista as mensagens recentes dos canais configurados.
3. O parser procura tipo da tarefa, nome do colaborador e data.
4. Quando os tres campos sao encontrados com seguranca, o bot:
   - copia o template correto do Trello com `idCardSource`
   - define o nome do card
   - define a data no card
   - adiciona um comentario com resumo, observacoes detectadas e link da mensagem no Discord
   - reage com `✅` na mensagem
5. Nos canais de pedido por mencao, quando alguem responde uma mensagem com algo como `@Bot, crie um card sobre isso para segunda feira`, o bot:
   - aceita tanto mencao direta ao usuario do bot quanto mencao ao cargo do bot no Discord
   - le a cadeia da mensagem respondida e, no minimo, todas as mensagens dos ultimos 60 minutos antes do pedido
   - recorta automaticamente onde o assunto comeca e termina com base em proximidade temporal, participantes e termos em comum
   - interpreta o prazo pedido
   - cria um card generico na lista de destino do Trello
   - adiciona comentario com o pedido e o contexto lido
   - reage com `✅`
6. Se estiver configurado `DISCORD_CONFIRMATION_MODE=reply` ou `both`, o bot tambem responde com o link do card.

O parser e conservador de proposito: se nome ou data nao forem detectados, ele nao cria card.

## O que voce precisa configurar

### Discord

1. Gerar o **Bot Token** no Developer Portal.
2. Instalar o bot no servidor certo.
3. Garantir estas permissoes no canal:
   - `VIEW_CHANNEL`
   - `READ_MESSAGE_HISTORY`
   - `ADD_REACTIONS`
   - `SEND_MESSAGES` se quiser resposta alem da reacao
4. Habilitar o **MESSAGE_CONTENT privileged intent** no app, porque sem isso o Discord retorna conteudo vazio para a mensagem.
5. Informar:
   - `DISCORD_BOT_TOKEN`
   - `DISCORD_GUILD_ID`
   - `DISCORD_CHANNEL_IDS` para canais de scan estruturado
   - `DISCORD_REQUEST_CHANNEL_IDS` se quiser canais dedicados para pedidos por mencao ao bot

### Trello

1. Criar ou usar um Power-Up para gerar a **API Key**.
2. Gerar o **API Token** da sua conta.
3. Informar:
   - `TRELLO_API_KEY`
   - `TRELLO_API_TOKEN`
4. Escolher uma destas formas de configuracao:
   - informar `TRELLO_TARGET_LIST_ID`, ou
   - informar `TRELLO_BOARD_REF` junto com `TRELLO_TARGET_LIST_NAME`
5. Informar os templates por ID ou URL do card:
   - `TRELLO_ONBOARDING_TEMPLATE_CARD_REF`
   - `TRELLO_OFFBOARDING_TEMPLATE_CARD_REF`

## Variaveis de ambiente

Copie `.env.example` para `.env` quando for rodar localmente.

| Variavel | Obrigatoria | Descricao |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | Sim | Token do bot do Discord |
| `DISCORD_GUILD_ID` | Sim | ID do servidor para montar o link da mensagem |
| `DISCORD_CHANNEL_IDS` | Condicional | IDs dos canais de scan estruturado, separados por virgula |
| `DISCORD_REQUEST_CHANNEL_IDS` | Nao | IDs dos canais onde o bot atende pedidos via `@Bot` |
| `DISCORD_CONFIRMATION_MODE` | Nao | `reaction`, `reply` ou `both` |
| `DISCORD_REACTION_EMOJI` | Nao | Emoji usado para confirmar, padrao `✅` |
| `DISCORD_REPLY_TEMPLATE` | Nao | Texto da resposta, com `{card_url}` |
| `TRELLO_API_KEY` | Sim | Chave da API do Trello |
| `TRELLO_API_TOKEN` | Sim | Token da API do Trello |
| `TRELLO_BOARD_REF` | Condicional | ID ou URL do board, usado para resolver a lista por nome |
| `TRELLO_TARGET_LIST_ID` | Condicional | ID da lista de destino no Trello |
| `TRELLO_TARGET_LIST_NAME` | Condicional | Nome da lista, usado com `TRELLO_BOARD_REF` |
| `TRELLO_ONBOARDING_TEMPLATE_CARD_REF` | Sim | ID ou URL do card template de onboarding |
| `TRELLO_OFFBOARDING_TEMPLATE_CARD_REF` | Sim | ID ou URL do card template de offboarding |
| `TRELLO_KEEP_FROM_SOURCE` | Nao | Campos copiados do template |
| `BOT_TIMEZONE` | Nao | Timezone IANA, padrao `America/Sao_Paulo` |
| `LOOKBACK_DAYS` | Nao | Janela de busca de mensagens |
| `MAX_MESSAGES_PER_CHANNEL` | Nao | Limite de mensagens paginadas por execucao |
| `LOG_LEVEL` | Nao | Nivel de log |

## Execucao local

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

Para uma verificacao manual com janela diferente:

```powershell
python main.py --lookback-days 14
```

## Hospedagem quase gratuita

A opcao mais simples para este caso e o **GitHub Actions**:

- serve bem para execucao diaria
- nao exige servidor ligado o tempo todo
- aceita segredos do repositorio
- o workflow deste projeto ja foi criado em `.github/workflows/daily_scan.yml`
- para pedidos por mencao ao bot em canal dedicado, existe tambem `.github/workflows/mention_requests.yml`

Assumi como horario padrao **09:17 em `America/Sao_Paulo`** para o scan diario. Para pedidos por mencao ao bot, o workflow dedicado roda **a cada 15 minutos**. Se voce quiser outro horario ou outra cadencia, basta ajustar o cron dos workflows.

## Segredos do GitHub Actions

No repositorio do GitHub, crie os seguintes repository secrets:

- `DISCORD_BOT_TOKEN`
- `DISCORD_GUILD_ID`
- `DISCORD_CHANNEL_IDS`
- `DISCORD_REQUEST_CHANNEL_IDS`
- `DISCORD_CONFIRMATION_MODE`
- `DISCORD_REACTION_EMOJI`
- `DISCORD_REPLY_TEMPLATE`
- `TRELLO_API_KEY`
- `TRELLO_API_TOKEN`
- `TRELLO_BOARD_REF`
- `TRELLO_TARGET_LIST_ID`
- `TRELLO_TARGET_LIST_NAME`
- `TRELLO_ONBOARDING_TEMPLATE_CARD_REF`
- `TRELLO_OFFBOARDING_TEMPLATE_CARD_REF`
- `TRELLO_KEEP_FROM_SOURCE`
- `BOT_TIMEZONE`
- `LOOKBACK_DAYS`
- `MAX_MESSAGES_PER_CHANNEL`
- `LOG_LEVEL`

Se preferir, voce pode preencher so os obrigatorios e deixar os opcionais sem criar secret. Nesse caso, remova do workflow as linhas dos segredos opcionais ou troque por valores fixos.

## Como pegar IDs rapidamente

- **Discord guild ID / channel ID**: habilite o modo desenvolvedor no Discord e use "Copiar ID".
- **Trello list ID / template card ID**: abra o board/card alvo e use a API do Trello para inspecionar o objeto correspondente.

## Limites atuais do parser

Sem exemplos reais das mensagens do seu canal, o parser foi deixado propositalmente conservador. Hoje ele funciona melhor quando a mensagem tem algo proximo de:

```text
Onboarding
Nome: Maria Silva
Data: 22/04/2026
Obs: quer monitor e teclado. Vai buscar no escritorio.
```

ou

```text
Offboarding Joao Souza dia 30/04/2026. Vai devolver por Uber.
```

Se as mensagens reais tiverem outro formato, me passe 2 ou 3 exemplos reais e eu ajusto o parser para esse padrao.

## Referencias oficiais

- Discord Bots / setup do app: https://docs.discord.com/developers/bots
- Discord installation / getting started: https://docs.discord.com/developers/quick-start/getting-started
- Discord Gateway intents: https://docs.discord.com/developers/events/gateway
- Discord Message resource: https://docs.discord.com/developers/resources/message
- Trello API intro: https://developer.atlassian.com/cloud/trello/guides/rest-api/api-introduction/
- Trello Cards REST API: https://developer.atlassian.com/cloud/trello/rest/api-group-cards/
- Trello OpenAPI oficial: https://dac-static.atlassian.com/cloud/trello/swagger.v3.json?_v=1.957.0
- GitHub Actions events/schedule: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- GitHub Actions secrets: https://docs.github.com/actions/security-guides/encrypted-secrets
- GitHub Actions billing: https://docs.github.com/en/billing/managing-billing-for-github-actions/about-billing-for-github-actions
