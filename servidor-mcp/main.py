#!/usr/bin/env python3
"""Servidor MCP para o Desafio A Ponte.

Porta 7301, Streamable HTTP no endpoint /mcp.
Expõe 3 tools (listar_salas, consultar_disponibilidade, reservar_sala) e 1 resource (politica://uso).
Implementa o ciclo MRTR com elicitation em form mode e requestState selado com HMAC.
"""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MCP_PORT", "7301"))
SECRET = os.environ.get("REQUEST_STATE_SECRET", "default_secret_32_bytes_minimum_string_123456789_mcp").encode()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SALAS_FILE = os.path.join(BASE_DIR, "dados", "salas.json")
RESERVAS_FILE = os.path.join(BASE_DIR, "dados", "reservas.json")
POLITICA_FILE = os.path.join(BASE_DIR, "dados", "politica-de-uso.md")


def load_salas() -> list[dict]:
    with open(SALAS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_initial_reservas() -> list[dict]:
    with open(RESERVAS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_politica() -> str:
    with open(POLITICA_FILE, "r", encoding="utf-8") as f:
        return f.read()


def extract_politica_version(politica_text: str) -> str:
    for line in politica_text.splitlines():
        if line.startswith("versao:"):
            return line.split(":", 1)[1].strip()
    return "2026-11-01"


# State in memory for new reservations created during server lifetime
IN_MEMORY_RESERVAS: list[dict] = []


def get_all_reservas() -> list[dict]:
    return load_initial_reservas() + IN_MEMORY_RESERVAS


def get_next_reserva_id() -> str:
    all_res = get_all_reservas()
    max_num = 0
    for r in all_res:
        rid = r.get("id", "")
        if rid.startswith("res-"):
            try:
                num = int(rid.split("-")[1])
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
    return f"res-{max_num + 1:04d}"


def seal_request_state(data: dict, ttl_seconds: int = 1800) -> str:
    payload = {
        "data": data,
        "exp": int(time.time()) + ttl_seconds
    }
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw_payload).decode("utf-8").rstrip("=")
    sig = hmac.new(SECRET, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")
    return f"v1.{payload_b64}.{sig_b64}"


def unseal_request_state(state_str: str) -> dict | None:
    try:
        parts = state_str.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return None
        payload_b64, sig_b64 = parts[1], parts[2]

        expected_sig = hmac.new(SECRET, payload_b64.encode("utf-8"), hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")
        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            return None

        rem = len(payload_b64) % 4
        if rem > 0:
            payload_b64_padded = payload_b64 + ("=" * (4 - rem))
        else:
            payload_b64_padded = payload_b64

        raw_payload = base64.urlsafe_b64decode(payload_b64_padded.encode("utf-8"))
        payload = json.loads(raw_payload.decode("utf-8"))

        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("data")
    except Exception:
        return None


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def validate_interval(sala_id: str, inicio_str: str, fim_str: str) -> str | None:
    salas = load_salas()
    sala_obj = next((s for s in salas if s["id"] == sala_id), None)
    if not sala_obj:
        return f"Sala inexistente: {sala_id}"

    try:
        dt_ini = parse_iso(inicio_str)
        dt_fim = parse_iso(fim_str)
    except Exception:
        return "Intervalo invalido: formato de data invalido"

    if dt_fim <= dt_ini:
        return "Intervalo invalido: fim deve ser posterior a inicio"

    duration_min = (dt_fim - dt_ini).total_seconds() / 60.0
    if duration_min > 120.0:
        return "Duracao acima do limite: a politica permite no maximo 2 horas"

    if (dt_ini.hour < 8 or dt_ini.hour > 20 or (dt_ini.hour == 20 and dt_ini.minute > 0) or
        dt_fim.hour < 8 or dt_fim.hour > 20 or (dt_fim.hour == 20 and dt_fim.minute > 0)):
        return "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"

    return None


def get_room_conflicts(sala_id: str, dt_ini: datetime, dt_fim: datetime) -> list[dict]:
    conflicts = []
    for r in get_all_reservas():
        if r.get("sala") == sala_id:
            r_ini = parse_iso(r["inicio"])
            r_fim = parse_iso(r["fim"])
            if dt_ini < r_fim and dt_fim > r_ini:
                conflicts.append(r)
    return conflicts


def find_alternative_rooms(requested_sala_id: str, dt_ini: datetime, dt_fim: datetime) -> list[str]:
    salas = load_salas()
    req_sala = next((s for s in salas if s["id"] == requested_sala_id), None)
    if not req_sala:
        return []

    candidates = []
    for s in salas:
        if s["id"] == requested_sala_id:
            continue
        if s["capacidade"] >= req_sala["capacidade"]:
            if not get_room_conflicts(s["id"], dt_ini, dt_fim):
                candidates.append(s)

    candidates.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidates[:3]]


SERVER_INFO = {
    "io.modelcontextprotocol/serverInfo": {
        "name": "central-de-salas",
        "version": "1.0.0"
    }
}

TOOLS_DEFINITIONS = [
    {
        "name": "listar_salas",
        "description": "Lista todas as salas com capacidade e recursos.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "title": "listar_salasArguments"
        },
        "outputSchema": {
            "$defs": {
                "SalaOut": {
                    "properties": {
                        "id": {"title": "Id", "type": "string"},
                        "nome": {"title": "Nome", "type": "string"},
                        "capacidade": {"title": "Capacidade", "type": "integer"},
                        "recursos": {
                            "items": {"type": "string"},
                            "title": "Recursos",
                            "type": "array"
                        }
                    },
                    "required": ["id", "nome", "capacidade", "recursos"],
                    "title": "SalaOut",
                    "type": "object"
                }
            },
            "properties": {
                "salas": {
                    "items": {"$ref": "#/$defs/SalaOut"},
                    "title": "Salas",
                    "type": "array"
                }
            },
            "required": ["salas"],
            "title": "ListaDeSalas",
            "type": "object"
        }
    },
    {
        "name": "consultar_disponibilidade",
        "description": "Diz se uma sala esta livre no intervalo, e quais reservas conflitam.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sala": {"title": "Sala", "type": "string"},
                "inicio": {"title": "Inicio", "type": "string"},
                "fim": {"title": "Fim", "type": "string"}
            },
            "required": ["sala", "inicio", "fim"],
            "title": "consultar_disponibilidadeArguments"
        },
        "outputSchema": {
            "$defs": {
                "ConflitoOut": {
                    "properties": {
                        "id": {"title": "Id", "type": "string"},
                        "inicio": {"title": "Inicio", "type": "string"},
                        "fim": {"title": "Fim", "type": "string"},
                        "responsavel": {"title": "Responsavel", "type": "string"}
                    },
                    "required": ["id", "inicio", "fim", "responsavel"],
                    "title": "ConflitoOut",
                    "type": "object"
                }
            },
            "properties": {
                "sala": {"title": "Sala", "type": "string"},
                "livre": {"title": "Livre", "type": "boolean"},
                "conflitos": {
                    "items": {"$ref": "#/$defs/ConflitoOut"},
                    "title": "Conflitos",
                    "type": "array"
                }
            },
            "required": ["sala", "livre", "conflitos"],
            "title": "Disponibilidade",
            "type": "object"
        }
    },
    {
        "name": "reservar_sala",
        "description": "Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sala": {"title": "Sala", "type": "string"},
                "inicio": {"title": "Inicio", "type": "string"},
                "fim": {"title": "Fim", "type": "string"},
                "responsavel": {"title": "Responsavel", "type": "string"}
            },
            "required": ["sala", "inicio", "fim", "responsavel"],
            "title": "reservar_salaArguments"
        },
        "outputSchema": {
            "properties": {
                "reserva": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Reserva"},
                "reservado": {"default": True, "title": "Reservado", "type": "boolean"},
                "sala": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Sala"},
                "inicio": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Inicio"},
                "fim": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Fim"},
                "responsavel": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Responsavel"},
                "politica": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Politica"},
                "motivo": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Motivo"}
            },
            "title": "ReservaOut",
            "type": "object"
        }
    }
]


class MCPHandler(BaseHTTPRequestHandler):
    def log_message(self, format_str: str, *args: object) -> None:
        # Suppress default HTTP logging to stdout/stderr unless needed
        pass

    def send_json_response(self, status: int, data: dict) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_jsonrpc_error(self, req_id: object, code: int, message: str, data: dict | None = None, status: int = 400) -> None:
        err: dict = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": err
        }
        self.send_json_response(status, resp)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/mcp":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self.send_jsonrpc_error(None, -32700, "Parse error", status=400)
            return

        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        meta = params.get("_meta") or {}

        # Traceparent logging to stderr
        traceparent = meta.get("traceparent") or self.headers.get("traceparent", "")
        sys.stderr.write(f"[MCP Server] method={method} id={req_id} traceparent={traceparent}\n")
        sys.stderr.flush()

        # Protocol version and client capabilities check
        if "io.modelcontextprotocol/protocolVersion" not in meta or "io.modelcontextprotocol/clientCapabilities" not in meta:
            self.send_jsonrpc_error(req_id, -32602, "Missing _meta fields: io.modelcontextprotocol/protocolVersion and clientCapabilities are required", status=400)
            return

        # Mcp-Method header check
        mcp_method = self.headers.get("Mcp-Method")
        if mcp_method and mcp_method != method:
            self.send_jsonrpc_error(req_id, -32020, f"Header Mcp-Method {mcp_method} does not match body method {method}", status=400)
            return

        if method == "tools/list":
            self.send_json_response(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "cacheScope": "private",
                    "resultType": "complete",
                    "tools": TOOLS_DEFINITIONS,
                    "ttlMs": 0,
                    "_meta": SERVER_INFO
                }
            })
            return

        elif method == "resources/read":
            uri = params.get("uri", "")
            if uri == "politica://uso":
                politica_content = load_politica()
                self.send_json_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "cacheScope": "private",
                        "contents": [
                            {
                                "mimeType": "text/markdown",
                                "text": politica_content,
                                "uri": "politica://uso"
                            }
                        ],
                        "resultType": "complete",
                        "ttlMs": 0,
                        "_meta": SERVER_INFO
                    }
                })
            else:
                self.send_jsonrpc_error(req_id, -32602, f"URI nao encontrada: {uri}", status=200)
            return

        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}

            if tool_name == "listar_salas":
                salas = load_salas()
                structured = {"salas": salas}
                text_json = json.dumps(structured)
                self.send_json_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"text": text_json, "type": "text"}],
                        "isError": False,
                        "resultType": "complete",
                        "structuredContent": structured,
                        "_meta": SERVER_INFO
                    }
                })
                return

            elif tool_name == "consultar_disponibilidade":
                sala = arguments.get("sala", "")
                inicio = arguments.get("inicio", "")
                fim = arguments.get("fim", "")

                err = validate_interval(sala, inicio, fim)
                if err:
                    self.send_json_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"text": err, "type": "text"}],
                            "isError": True,
                            "resultType": "complete",
                            "_meta": SERVER_INFO
                        }
                    })
                    return

                dt_ini = parse_iso(inicio)
                dt_fim = parse_iso(fim)
                conflicts = get_room_conflicts(sala, dt_ini, dt_fim)
                livre = len(conflicts) == 0

                c_out = []
                for c in conflicts:
                    c_out.append({
                        "id": c.get("id", ""),
                        "inicio": c.get("inicio", ""),
                        "fim": c.get("fim", ""),
                        "responsavel": c.get("responsavel", "")
                    })

                structured = {
                    "sala": sala,
                    "livre": livre,
                    "conflitos": c_out
                }
                text_json = json.dumps(structured)

                self.send_json_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"text": text_json, "type": "text"}],
                        "isError": False,
                        "resultType": "complete",
                        "structuredContent": structured,
                        "_meta": SERVER_INFO
                    }
                })
                return

            elif tool_name == "reservar_sala":
                # Check if this is a RETRY (contains requestState)
                request_state_input = params.get("requestState")
                input_responses = params.get("inputResponses") or {}

                if request_state_input:
                    # Validate requestState signature & expiration
                    sealed_data = unseal_request_state(request_state_input)
                    if not sealed_data:
                        self.send_jsonrpc_error(req_id, -32602, "requestState invalido ou expirado", status=200)
                        return

                    # Override arguments with sealed parameters to prevent tampering
                    orig_sala = sealed_data.get("sala")
                    orig_inicio = sealed_data.get("inicio")
                    orig_fim = sealed_data.get("fim")
                    orig_responsavel = sealed_data.get("responsavel")

                    # Extract input response
                    resp_entry = None
                    if input_responses:
                        resp_entry = next(iter(input_responses.values()), None)

                    action = resp_entry.get("action") if resp_entry else None

                    if action in ("decline", "cancel"):
                        structured = {
                            "reserva": None,
                            "reservado": False,
                            "sala": None,
                            "inicio": None,
                            "fim": None,
                            "responsavel": None,
                            "politica": None,
                            "motivo": "recusado"
                        }
                        text_json = json.dumps(structured, indent=2)
                        self.send_json_response(200, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "content": [{"text": text_json, "type": "text"}],
                                "isError": False,
                                "resultType": "complete",
                                "structuredContent": structured,
                                "_meta": SERVER_INFO
                            }
                        })
                        return
                    else:
                        chosen_sala = ((resp_entry.get("content") or {}).get("sala")) if resp_entry else None
                        if not chosen_sala:
                            chosen_sala = orig_sala

                        res_id = get_next_reserva_id()
                        politica_ver = extract_politica_version(load_politica())

                        new_res = {
                            "id": res_id,
                            "sala": chosen_sala,
                            "inicio": orig_inicio,
                            "fim": orig_fim,
                            "responsavel": orig_responsavel
                        }
                        IN_MEMORY_RESERVAS.append(new_res)

                        structured = {
                            "reserva": res_id,
                            "reservado": True,
                            "sala": chosen_sala,
                            "inicio": orig_inicio,
                            "fim": orig_fim,
                            "responsavel": orig_responsavel,
                            "politica": politica_ver,
                            "motivo": None
                        }
                        text_json = json.dumps(structured, indent=2)

                        self.send_json_response(200, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "content": [{"text": text_json, "type": "text"}],
                                "isError": False,
                                "resultType": "complete",
                                "structuredContent": structured,
                                "_meta": SERVER_INFO
                            }
                        })
                        return

                # Standard initial booking request (NOT a retry)
                sala = arguments.get("sala", "")
                inicio = arguments.get("inicio", "")
                fim = arguments.get("fim", "")
                responsavel = arguments.get("responsavel", "")

                err = validate_interval(sala, inicio, fim)
                if err:
                    self.send_json_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"text": err, "type": "text"}],
                            "isError": True,
                            "resultType": "complete",
                            "_meta": SERVER_INFO
                        }
                    })
                    return

                dt_ini = parse_iso(inicio)
                dt_fim = parse_iso(fim)

                conflicts = get_room_conflicts(sala, dt_ini, dt_fim)

                if not conflicts:
                    # Free to book
                    res_id = get_next_reserva_id()
                    politica_ver = extract_politica_version(load_politica())
                    new_res = {
                        "id": res_id,
                        "sala": sala,
                        "inicio": inicio,
                        "fim": fim,
                        "responsavel": responsavel
                    }
                    IN_MEMORY_RESERVAS.append(new_res)

                    structured = {
                        "reserva": res_id,
                        "reservado": True,
                        "sala": sala,
                        "inicio": inicio,
                        "fim": fim,
                        "responsavel": responsavel,
                        "politica": politica_ver,
                        "motivo": None
                    }
                    text_json = json.dumps(structured, indent=2)

                    self.send_json_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"text": text_json, "type": "text"}],
                            "isError": False,
                            "resultType": "complete",
                            "structuredContent": structured,
                            "_meta": SERVER_INFO
                        }
                    })
                    return
                else:
                    # Occupied -> MRTR input_required cycle
                    # Check if client declared form elicitation capability
                    client_caps = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
                    elicitation_cap = client_caps.get("elicitation") or {}
                    if "form" not in elicitation_cap:
                        self.send_jsonrpc_error(
                            req_id,
                            -32021,
                            "Client did not declare the form elicitation capability required by resolver '__main__:escolha_de_sala'",
                            data={"requiredCapabilities": {"elicitation": {"form": {}}}},
                            status=400
                        )
                        return

                    alternatives = find_alternative_rooms(sala, dt_ini, dt_fim)
                    if not alternatives:
                        self.send_json_response(200, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "content": [{"text": "Sem alternativas disponiveis no intervalo", "type": "text"}],
                                "isError": True,
                                "resultType": "complete",
                                "_meta": SERVER_INFO
                            }
                        })
                        return

                    # Seal requestState
                    state_data = {
                        "sala": sala,
                        "inicio": inicio,
                        "fim": fim,
                        "responsavel": responsavel
                    }
                    request_state_str = seal_request_state(state_data)

                    elicitation_key = "__main__:escolha_de_sala"
                    input_requests = {
                        elicitation_key: {
                            "method": "elicitation/create",
                            "params": {
                                "message": "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                                "mode": "form",
                                "requestedSchema": {
                                    "type": "object",
                                    "properties": {
                                        "sala": {
                                            "description": "Sala alternativa escolhida",
                                            "enum": alternatives,
                                            "title": "Sala",
                                            "type": "string"
                                        }
                                    },
                                    "required": ["sala"]
                                }
                                if len(alternatives) > 1
                                else {
                                    "type": "object",
                                    "properties": {
                                        "sala": {
                                            "description": "Sala alternativa escolhida",
                                            "const": alternatives[0],
                                            "title": "Sala",
                                            "type": "string"
                                        }
                                    },
                                    "required": ["sala"]
                                }
                            }
                        }
                    }

                    self.send_json_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "inputRequests": input_requests,
                            "requestState": request_state_str,
                            "resultType": "input_required",
                            "_meta": SERVER_INFO
                        }
                    })
                    return

            else:
                # Unknown tool name
                self.send_jsonrpc_error(req_id, -32602, f"Tool nao encontrada: {tool_name}", status=200)
                return

        else:
            self.send_jsonrpc_error(req_id, -32601, f"Method not found: {method}", status=200)
            return


def run_server() -> None:
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, MCPHandler)
    sys.stderr.write(f"Servidor MCP rodando na porta {PORT}...\n")
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_server()
