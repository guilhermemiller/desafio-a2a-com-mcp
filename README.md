# A Ponte: um agente A2A com MCP por dentro

Este repositório contém a solução do desafio **A Ponte**, implementando um Servidor MCP e um Agente A2A em Python 3.10+ utilizando puramente a biblioteca padrão do Python (sem dependências externas de pacotes ou modelos de linguagem).

---

## 1. Como rodar

A partir de um clone limpo do repositório:

### Passo 1: Configurar a chave secreta de integridade
Exporte a variável de ambiente `REQUEST_STATE_SECRET` com no mínimo 32 bytes de aleatoriedade:
```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

### Passo 2: Iniciar os dois processos
Em um terminal, inicie o **Servidor MCP** (porta `7301`):
```bash
python3 servidor-mcp/main.py
```

Em outro terminal, inicie o **Agente A2A** (porta `7300`):
```bash
python3 agente/main.py
```

### Passo 3: Executar o validador de conformidade
Com ambos os processos ativos, rode o validador oficial:
```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Alternativamente, você pode rodar ambos os serviços e o validador de forma automatizada com o script:
```bash
chmod +x run_validador.sh
./run_validador.sh
```

---

## 2. Onde a ponte acontece

A "ponte" entre a pausa por solicitação de informação do protocolo MCP (**MRTR**) e o gerenciamento de estados de tarefas do protocolo A2A (**Task**) ocorre no arquivo `agente/main.py`:

- **Pausa (`input_required` -> `TASK_STATE_INPUT_REQUIRED`)**:
  No arquivo `agente/main.py`, dentro da função `do_POST()` ao processar o método `SendMessage`. Quando o servidor MCP retorna `resultType == "input_required"`, o agente extrai a lista de alternativas do schema da elicitation e transiciona a Task para `TASK_STATE_INPUT_REQUIRED`. A mensagem da Task é formatada exatamente como `alternativas: <ids_ordenados>`.
  O token `requestState` opaco e os argumentos originais são salvos exclusivamente no campo privado `task["_internal"]` da Task em memória, nunca sendo expostos em respostas A2A.

- **Retomada (`escolha=` -> Retry MCP com ID novo)**:
  Ainda no `agente/main.py`, ao receber a mensagem de continuação com `taskId`. O agente verifica a resposta (`escolha=<id>` ou `escolha=recusar`). Em seguida, emite uma nova requisição `tools/call` ao servidor MCP com um **ID JSON-RPC novo e distinto**, enviando `inputResponses` (`action: "accept"` com a sala escolhida ou `action: "decline"`) acompanhado do `requestState` recuperado da memória interna da Task. O servidor MCP valida o estado, desfaz o lacre e conclui a reserva.

---

## 3. Decisões técnicas

- **Proteção do `requestState`**:
  O `requestState` é gerado no `servidor-mcp/main.py` através da função `seal_request_state()`. Ele utiliza **HMAC-SHA256** derivado da chave `REQUEST_STATE_SECRET`. O payload lacrado contém os argumentos originais (`sala`, `inicio`, `fim`, `responsavel`) e a expiração do token (`exp` de 30 minutos). O formato final gerado é `v1.<payload_b64url>.<assinatura_b64url>`.

- **Integridade & Proteção Contra Adulteração**:
  No retry, o servidor MCP revalida a assinatura HMAC e o tempo de expiração (`unseal_request_state()`). Se o token for adulterado, o servidor rejeita a chamada com erro `-32602`. Além disso, o servidor sobrepõe quaisquer argumentos reenviados pelos valores selados dentro do `requestState`, garantindo imutabilidade contra modificações do cliente.

- **Isolamento de Estado das Tasks no Agente**:
  O agente armazena as Tasks no dicionário `TASKS`. Os metadados privados (`requestState`, `elicitationKey`, `origArgs`, `alternatives`) ficam isolados sob a chave `_internal`. Antes de serializar e responder qualquer requisição A2A (`GetTask` ou `SendMessage`), a função `clean_task_for_a2a()` remove o campo `_internal`, assegurando que o `requestState` jamais vaze nas respostas A2A (atendendo à verificação 34 do validador).

- **Arquitetura Determinística e Sem Dependências Externas**:
  Toda a solução foi escrita utilizando puramente a biblioteca padrão do Python 3.10+ (`http.server.ThreadingHTTPServer`, `urllib.request`, `json`, `hmac`, `hashlib`, `secrets`, `datetime`), sem frameworks pesados, ORMs ou modelos de linguagem (LLM) no fluxo de execução.

---

## 4. Saída do validador

```text
trace-id desta execucao: 0123456789abcdef0123456789abcdef
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata
PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
