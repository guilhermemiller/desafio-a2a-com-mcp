#!/usr/bin/env python3
"""Agente A2A e Host MCP para o Desafio A Ponte.

Porta 7300.
Card publicado em /.well-known/agent-card.json.
Endpoint A2A JSON-RPC em /a2a.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("AGENTE_PORT", "7300"))
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

# In-memory storage for tasks
TASKS: dict[str, dict] = {}

# MCP cache
POLITICA_VERSION: str = "2026-11-01"


def call_mcp(method: str, params: dict, tool_or_resource_name: str | None = None, traceparent: str | None = None) -> tuple[int, dict]:
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {
            "name": "agente-central-de-salas",
            "version": "1.0.0"
        },
        "io.modelcontextprotocol/clientCapabilities": {
            "elicitation": {
                "form": {}
            }
        }
    }
    if traceparent:
        meta["traceparent"] = traceparent

    body = {
        "jsonrpc": "2.0",
        "id": secrets.token_hex(6),
        "method": method,
        "params": {
            **params,
            "_meta": meta
        }
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method
    }
    if tool_or_resource_name:
        headers["Mcp-Name"] = tool_or_resource_name
    if traceparent:
        headers["traceparent"] = traceparent

    raw_data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=raw_data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8")
            return resp.status, json.loads(resp_body)
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(resp_body)
        except Exception:
            return e.code, {"_raw": resp_body}
    except Exception as e:
        return 500, {"error": {"code": -32603, "message": str(e)}}


def init_mcp_host(traceparent: str | None = None) -> None:
    global POLITICA_VERSION
    # 1. Discover tools
    call_mcp("tools/list", {}, traceparent=traceparent)

    # 2. Read policy resource
    _, resp = call_mcp("resources/read", {"uri": "politica://uso"}, "politica://uso", traceparent=traceparent)
    contents = (resp.get("result") or {}).get("contents") or []
    if contents:
        text = contents[0].get("text", "")
        for line in text.splitlines():
            if line.startswith("versao:"):
                POLITICA_VERSION = line.split(":", 1)[1].strip()


def parse_booking_prompt(text: str) -> dict | None:
    parts = text.strip().split()
    args = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            args[k.strip()] = v.strip()
    if "sala" in args and "inicio" in args and "fim" in args and "responsavel" in args:
        return args
    return None


def clean_task_for_a2a(task: dict) -> dict:
    cleaned = json.loads(json.dumps(task))
    cleaned.pop("_internal", None)
    return cleaned


AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {
        "organization": "Hill Valley Tech",
        "url": "https://hillvalley.example"
    },
    "version": "1.0.0",
    "supportedInterfaces": [
        {
            "url": f"http://127.0.0.1:{PORT}/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0"
        }
    ],
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "extendedAgentCard": False
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ]
        }
    ]
}


class A2AHandler(BaseHTTPRequestHandler):
    def log_message(self, format_str: str, *args: object) -> None:
        pass

    def send_json_response(self, status: int, data: dict) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_jsonrpc_error(self, req_id: object, code: int, message: str, status: int = 200) -> None:
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": message
            }
        }
        self.send_json_response(status, resp)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/.well-known/agent-card.json":
            self.send_json_response(200, AGENT_CARD)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/a2a":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self.send_jsonrpc_error(None, -32700, "Parse error")
            return

        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        traceparent = self.headers.get("traceparent")

        if method == "GetTask":
            task_id = params.get("id")
            task = TASKS.get(task_id)
            if not task:
                self.send_jsonrpc_error(req_id, -32602, f"Task {task_id} nao encontrada")
                return

            self.send_json_response(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "task": clean_task_for_a2a(task)
                }
            })
            return

        elif method == "SendMessage":
            message_obj = params.get("message") or {}
            msg_id = message_obj.get("messageId") or f"msg-{secrets.token_hex(6)}"
            parts = message_obj.get("parts") or []
            input_text = " ".join(p.get("text", "") for p in parts)
            task_id = message_obj.get("taskId")

            # Init host if needed
            init_mcp_host(traceparent=traceparent)

            # CASE 1: CONTINUATION OF EXISTING TASK
            if task_id:
                task = TASKS.get(task_id)
                if not task:
                    self.send_jsonrpc_error(req_id, -32602, f"Task {task_id} nao encontrada")
                    return

                curr_state = task["status"]["state"]
                if curr_state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"):
                    self.send_jsonrpc_error(req_id, -32602, "Task em estado terminal nao aceita novas mensagens")
                    return

                if curr_state == "TASK_STATE_INPUT_REQUIRED":
                    internal = task["_internal"]
                    alternatives = internal.get("alternatives", [])
                    request_state = internal.get("requestState")
                    elicitation_key = internal.get("elicitationKey")
                    orig_args = internal.get("origArgs", {})

                    user_msg = {
                        "messageId": msg_id,
                        "role": "ROLE_USER",
                        "parts": [{"text": input_text}],
                        "taskId": task_id
                    }
                    task["history"].append(user_msg)

                    # Extract choice
                    choice = input_text.strip()
                    if "=" in choice:
                        choice = choice.split("=", 1)[1].strip()

                    if choice == "recusar":
                        # Decline retry
                        mcp_params = {
                            "name": "reservar_sala",
                            "arguments": orig_args,
                            "inputResponses": {
                                elicitation_key: {"action": "decline"}
                            },
                            "requestState": request_state
                        }
                        call_mcp("tools/call", mcp_params, "reservar_sala", traceparent=traceparent)

                        agent_msg_id = f"msg-{secrets.token_hex(6)}"
                        agent_text = "Reserva cancelada pelo usuario."
                        agent_msg = {
                            "messageId": agent_msg_id,
                            "role": "ROLE_AGENT",
                            "parts": [{"text": agent_text}],
                            "taskId": task_id,
                            "contextId": task["contextId"]
                        }
                        task["status"]["state"] = "TASK_STATE_CANCELED"
                        task["status"]["message"] = agent_msg
                        task["history"].append(agent_msg)

                        self.send_json_response(200, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "task": clean_task_for_a2a(task)
                            }
                        })
                        return

                    elif choice in alternatives:
                        # Accept choice retry
                        mcp_params = {
                            "name": "reservar_sala",
                            "arguments": orig_args,
                            "inputResponses": {
                                elicitation_key: {
                                    "action": "accept",
                                    "content": {"sala": choice}
                                }
                            },
                            "requestState": request_state
                        }
                        _, mcp_resp = call_mcp("tools/call", mcp_params, "reservar_sala", traceparent=traceparent)
                        result = mcp_resp.get("result") or {}
                        sc = result.get("structuredContent") or {}
                        res_id = sc.get("reserva", "res-0000")

                        agent_msg_id = f"msg-{secrets.token_hex(6)}"
                        agent_text = f"Reserva {res_id} confirmada na {choice}."
                        agent_msg = {
                            "messageId": agent_msg_id,
                            "role": "ROLE_AGENT",
                            "parts": [{"text": agent_text}],
                            "taskId": task_id,
                            "contextId": task["contextId"]
                        }

                        artifact = {
                            "artifactId": f"art-{secrets.token_hex(6)}",
                            "name": "reserva",
                            "parts": [
                                {
                                    "text": json.dumps({
                                        "reserva": res_id,
                                        "sala": choice,
                                        "inicio": sc.get("inicio") or orig_args.get("inicio"),
                                        "fim": sc.get("fim") or orig_args.get("fim"),
                                        "responsavel": sc.get("responsavel") or orig_args.get("responsavel"),
                                        "politica": POLITICA_VERSION
                                    })
                                }
                            ]
                        }

                        task["status"]["state"] = "TASK_STATE_COMPLETED"
                        task["status"]["message"] = agent_msg
                        task["history"].append(agent_msg)
                        task["artifacts"] = [artifact]

                        self.send_json_response(200, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "task": clean_task_for_a2a(task)
                            }
                        })
                        return

                    else:
                        # Invalid choice -> keep paused
                        agent_msg_id = f"msg-{secrets.token_hex(6)}"
                        agent_text = f"alternativas: {', '.join(alternatives)}"
                        agent_msg = {
                            "messageId": agent_msg_id,
                            "role": "ROLE_AGENT",
                            "parts": [{"text": agent_text}],
                            "taskId": task_id,
                            "contextId": task["contextId"]
                        }
                        task["status"]["state"] = "TASK_STATE_INPUT_REQUIRED"
                        task["status"]["message"] = agent_msg
                        task["history"].append(agent_msg)

                        self.send_json_response(200, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "task": clean_task_for_a2a(task)
                            }
                        })
                        return

            # CASE 2: NEW TASK
            new_task_id = f"task-{secrets.token_hex(6)}"
            new_context_id = f"ctx-{secrets.token_hex(6)}"

            user_msg = {
                "messageId": msg_id,
                "role": "ROLE_USER",
                "parts": [{"text": input_text}]
            }

            parsed_args = parse_booking_prompt(input_text)
            if not parsed_args:
                agent_msg_id = f"msg-{secrets.token_hex(6)}"
                agent_text = "Formato de pedido invalido."
                agent_msg = {
                    "messageId": agent_msg_id,
                    "role": "ROLE_AGENT",
                    "parts": [{"text": agent_text}],
                    "taskId": new_task_id,
                    "contextId": new_context_id
                }
                task = {
                    "id": new_task_id,
                    "contextId": new_context_id,
                    "status": {
                        "state": "TASK_STATE_FAILED",
                        "message": agent_msg
                    },
                    "history": [user_msg, agent_msg],
                    "artifacts": []
                }
                TASKS[new_task_id] = task
                self.send_json_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "task": clean_task_for_a2a(task)
                    }
                })
                return

            # Call MCP reservar_sala
            mcp_params = {
                "name": "reservar_sala",
                "arguments": parsed_args
            }
            _, mcp_resp = call_mcp("tools/call", mcp_params, "reservar_sala", traceparent=traceparent)
            result = mcp_resp.get("result") or {}

            # Execution error (isError: True)
            if result.get("isError"):
                err_text = " ".join(p.get("text", "") for p in result.get("content", []))
                agent_msg_id = f"msg-{secrets.token_hex(6)}"
                agent_msg = {
                    "messageId": agent_msg_id,
                    "role": "ROLE_AGENT",
                    "parts": [{"text": err_text}],
                    "taskId": new_task_id,
                    "contextId": new_context_id
                }
                task = {
                    "id": new_task_id,
                    "contextId": new_context_id,
                    "status": {
                        "state": "TASK_STATE_FAILED",
                        "message": agent_msg
                    },
                    "history": [user_msg, agent_msg],
                    "artifacts": []
                }
                TASKS[new_task_id] = task
                self.send_json_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "task": clean_task_for_a2a(task)
                    }
                })
                return

            # Complete directly (no conflict)
            if result.get("resultType") == "complete":
                sc = result.get("structuredContent") or {}
                res_id = sc.get("reserva", "res-0000")
                sala = sc.get("sala") or parsed_args.get("sala")

                agent_msg_id = f"msg-{secrets.token_hex(6)}"
                agent_text = f"Reserva {res_id} confirmada na {sala}."
                agent_msg = {
                    "messageId": agent_msg_id,
                    "role": "ROLE_AGENT",
                    "parts": [{"text": agent_text}],
                    "taskId": new_task_id,
                    "contextId": new_context_id
                }

                artifact = {
                    "artifactId": f"art-{secrets.token_hex(6)}",
                    "name": "reserva",
                    "parts": [
                        {
                            "text": json.dumps({
                                "reserva": res_id,
                                "sala": sala,
                                "inicio": sc.get("inicio") or parsed_args.get("inicio"),
                                "fim": sc.get("fim") or parsed_args.get("fim"),
                                "responsavel": sc.get("responsavel") or parsed_args.get("responsavel"),
                                "politica": POLITICA_VERSION
                            })
                        }
                    ]
                }

                task = {
                    "id": new_task_id,
                    "contextId": new_context_id,
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": agent_msg
                    },
                    "history": [user_msg, agent_msg],
                    "artifacts": [artifact]
                }
                TASKS[new_task_id] = task
                self.send_json_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "task": clean_task_for_a2a(task)
                    }
                })
                return

            # Paused (input_required)
            if result.get("resultType") == "input_required":
                input_requests = result.get("inputRequests") or {}
                elicitation_key = next(iter(input_requests.keys()), "__main__:escolha_de_sala")
                req_params = (input_requests.get(elicitation_key) or {}).get("params") or {}
                req_schema = req_params.get("requestedSchema") or {}
                prop = (req_schema.get("properties") or {}).get("sala") or {}

                alternatives = prop.get("enum") or ([prop["const"]] if "const" in prop else [])

                request_state = result.get("requestState")

                agent_msg_id = f"msg-{secrets.token_hex(6)}"
                agent_text = f"alternativas: {', '.join(alternatives)}"
                agent_msg = {
                    "messageId": agent_msg_id,
                    "role": "ROLE_AGENT",
                    "parts": [{"text": agent_text}],
                    "taskId": new_task_id,
                    "contextId": new_context_id
                }

                task = {
                    "id": new_task_id,
                    "contextId": new_context_id,
                    "status": {
                        "state": "TASK_STATE_INPUT_REQUIRED",
                        "message": agent_msg
                    },
                    "history": [user_msg, agent_msg],
                    "artifacts": [],
                    "_internal": {
                        "requestState": request_state,
                        "elicitationKey": elicitation_key,
                        "origArgs": parsed_args,
                        "alternatives": alternatives
                    }
                }
                TASKS[new_task_id] = task

                self.send_json_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "task": clean_task_for_a2a(task)
                    }
                })
                return

        else:
            self.send_jsonrpc_error(req_id, -32601, f"Method not found: {method}")


def run_agent() -> None:
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, A2AHandler)
    sys.stderr.write(f"Agente A2A rodando na porta {PORT}...\n")
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_agent()
