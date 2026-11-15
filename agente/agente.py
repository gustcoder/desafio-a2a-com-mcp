"""Agente da Central de Salas: servidor A2A v1.0 por fora, host MCP por dentro.

    uv run python agente.py

- GET  /.well-known/agent-card.json  -> Agent Card (v1.0)
- POST /a2a                          -> JSON-RPC 2.0: SendMessage e GetTask

Sem LLM: o pedido tem formato fixo e a decisão é por regra. Quem chama não
tem como saber disso (opacidade do A2A), e o resultado é determinístico.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cliente_mcp import ClienteMCP, Trace
from ponte import Ponte
from tarefas import INPUT_REQUIRED, RegistroDeTarefas

HOST = os.environ.get("A2A_HOST", "127.0.0.1")
PORTA = int(os.environ.get("A2A_PORT", "7300"))
URL_A2A = os.environ.get("A2A_URL", f"http://{HOST}:{PORTA}/a2a")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

# Códigos JSON-RPC: os do JSON-RPC 2.0 e os específicos do A2A.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


AGENT_CARD: dict[str, Any] = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    # v1.0: url, binding e versão de protocolo vivem dentro de supportedInterfaces.
    "supportedInterfaces": [{"url": URL_A2A, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
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
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


class ErroJsonRpc(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


registro = RegistroDeTarefas()
# Uma trava por Task: duas mensagens para a mesma Task não se atropelam.
travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    partes = mensagem.get("parts")
    if not isinstance(partes, list) or not partes:
        raise ErroJsonRpc(INVALID_PARAMS, "message.parts deve ser uma lista nao vazia")
    textos = [p["text"] for p in partes if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if not textos:
        raise ErroJsonRpc(INVALID_PARAMS, "message.parts sem parte de texto")
    return " ".join(textos)


def _history_length(params: dict[str, Any]) -> int | None:
    valor = params.get("historyLength", (params.get("configuration") or {}).get("historyLength"))
    return valor if isinstance(valor, int) and valor >= 0 else None


async def send_message(params: dict[str, Any], request: Request) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("messageId"), str):
        raise ErroJsonRpc(INVALID_PARAMS, "params.message com messageId e obrigatorio")
    if mensagem.get("role") != "ROLE_USER":
        raise ErroJsonRpc(INVALID_PARAMS, "message.role deve ser ROLE_USER")
    texto = _texto_da_mensagem(mensagem)
    ponte: Ponte = request.app.state.ponte
    task_id = mensagem.get("taskId")

    if task_id:
        # Continuação de uma Task existente.
        tarefa = registro.obter(task_id)
        if tarefa is None:
            raise ErroJsonRpc(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        async with travas[task_id]:
            if tarefa.terminal:
                # Estado terminal é definitivo: nada volta a WORKING.
                raise ErroJsonRpc(
                    UNSUPPORTED_OPERATION,
                    f"Task {task_id} esta em estado terminal ({tarefa.estado}) e nao aceita novas mensagens",
                )
            if tarefa.estado != INPUT_REQUIRED:
                raise ErroJsonRpc(UNSUPPORTED_OPERATION, f"Task {task_id} nao esta aguardando entrada")
            if mensagem.get("contextId") not in (None, tarefa.context_id):
                raise ErroJsonRpc(INVALID_PARAMS, "contextId nao corresponde ao da Task")
            tarefa.registrar_usuario(mensagem)
            await ponte.continuar(tarefa, texto)
    else:
        # Sem taskId: Task nova, com id e contextId próprios.
        tarefa = registro.criar(mensagem.get("contextId"))
        async with travas[tarefa.id]:
            tarefa.registrar_usuario(mensagem)
            # O trace-id do cliente acompanha todos os requests MCP desta Task.
            trace = Trace.de_header(request.headers.get("traceparent"))
            await ponte.iniciar(tarefa, texto, trace)
    return {"task": tarefa.para_wire(_history_length(params))}


async def get_task(params: dict[str, Any], request: Request) -> dict[str, Any]:
    task_id = params.get("id")
    if not isinstance(task_id, str):
        raise ErroJsonRpc(INVALID_PARAMS, "params.id e obrigatorio")
    tarefa = registro.obter(task_id)
    if tarefa is None:
        raise ErroJsonRpc(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
    # Sem trava: GetTask reflete o estado corrente, inclusive WORKING no meio de uma chamada MCP.
    return {"task": tarefa.para_wire(_history_length(params))}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


def _resposta(id_: Any, *, result: Any = None, erro: ErroJsonRpc | None = None) -> JSONResponse:
    corpo: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if erro is None:
        corpo["result"] = result
    else:
        corpo["error"] = {"code": erro.code, "message": erro.message}
        if erro.data is not None:
            corpo["error"]["data"] = erro.data
    return JSONResponse(corpo)


async def endpoint_a2a(request: Request) -> JSONResponse:
    try:
        corpo = json.loads(await request.body())
    except ValueError:
        return _resposta(None, erro=ErroJsonRpc(PARSE_ERROR, "JSON invalido"))
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return _resposta(None, erro=ErroJsonRpc(INVALID_REQUEST, "request JSON-RPC 2.0 invalido"))
    id_, metodo = corpo.get("id"), corpo["method"]
    params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
    alvo = params.get("id") or (params.get("message") or {}).get("taskId") or "-"
    log(f"[a2a] method={metodo} id={json.dumps(id_)} task={alvo} traceparent={request.headers.get('traceparent', '-')}")

    handler = METODOS.get(metodo)
    if handler is None:
        return _resposta(id_, erro=ErroJsonRpc(METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}"))
    try:
        return _resposta(id_, result=await handler(params, request))
    except ErroJsonRpc as e:
        return _resposta(id_, erro=e)
    except Exception as e:  # nada de stack trace para o cliente
        log(f"[a2a] erro interno: {e!r}")
        return _resposta(id_, erro=ErroJsonRpc(INTERNAL_ERROR, "Erro interno do agente"))


async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


@asynccontextmanager
async def ciclo_de_vida(app: Starlette):
    # Um cliente MCP vivo durante todo o processo (reuso de conexão, não sessão).
    async with ClienteMCP(MCP_URL) as mcp:
        app.state.ponte = Ponte(mcp)
        log(f"[a2a] Central de Salas em {URL_A2A} (card em /.well-known/agent-card.json), MCP em {MCP_URL}")
        yield


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", endpoint_a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
