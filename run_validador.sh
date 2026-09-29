#!/usr/bin/env bash
set -e

export REQUEST_STATE_SECRET=${REQUEST_STATE_SECRET:-$(python3 -c "import secrets; print(secrets.token_hex(32))")}

echo "Iniciando Servidor MCP (porta 7301)..."
python3 servidor-mcp/main.py &
MCP_PID=$!

echo "Iniciando Agente A2A (porta 7300)..."
python3 agente/main.py &
AGENTE_PID=$!

cleanup() {
  echo "Encerrando processos..."
  kill $MCP_PID $AGENTE_PID 2>/dev/null || true
}
trap cleanup EXIT

sleep 2

echo "Executando validador..."
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
