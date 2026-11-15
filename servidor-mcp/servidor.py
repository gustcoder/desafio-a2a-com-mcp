"""Servidor MCP da Central de Salas (Streamable HTTP, revisão 2026-07-28).

    REQUEST_STATE_SECRET=<hex de 32 bytes> python servidor.py

Três tools, um resource e o ciclo de MRTR em `reservar_sala`. As regras de
domínio vivem em dominio.py; aqui está só a tradução para o protocolo.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Any, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

import dominio

PORTA = int(os.environ.get("MCP_PORT", "7301"))
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
# Validade do requestState: 10 minutos, dentro da faixa de 5 a 30 exigida.
TTL_REQUEST_STATE = 600.0


def log(mensagem: str) -> None:
    """stderr, e não o logging do protocolo (depreciado na 2026-07-28)."""
    print(mensagem, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Proteção do requestState
# ---------------------------------------------------------------------------

def carregar_segredo() -> bytes:
    """Lê REQUEST_STATE_SECRET do ambiente. Nunca há valor padrão no código."""
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        sys.exit(
            "REQUEST_STATE_SECRET nao definido. Gere com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    try:
        segredo = bytes.fromhex(bruto)
    except ValueError:
        segredo = bruto.encode()
    if len(segredo) < 32:
        sys.exit(f"REQUEST_STATE_SECRET precisa de pelo menos 32 bytes; veio {len(segredo)}.")
    return segredo


# O SDK sela todo requestState que sai em um envelope AES-256-GCM (AEAD: cifra e
# autentica) com expiração, método, nome da tool, digest dos argumentos e
# audiência (o nome do servidor). Na volta ele verifica tudo antes do handler
# rodar e responde -32602 se algo não bater. Com a chave vinda do ambiente,
# um estado emitido antes de um restart continua válido depois dele.
seguranca = RequestStateSecurity(keys=[carregar_segredo()], ttl=TTL_REQUEST_STATE)

mcp = MCPServer("central-de-salas", version="1.0.0", request_state_security=seguranca)


# ---------------------------------------------------------------------------
# Schemas de saída (viram outputSchema e structuredContent)
# ---------------------------------------------------------------------------

class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class Escolha(BaseModel):
    """Forma da resposta à elicitation. O schema real, com o enum, é gerado por pedido."""

    sala: str


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _validar(sala: str, inicio: str, fim: str):
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.ErroDeRegra as e:
        # ToolError vira um resultado `complete` com isError: true.
        raise ToolError(str(e)) from None


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in dominio.SALAS.values()])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = _validar(sala, inicio, fim)
    em_conflito = dominio.conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel) for r in em_conflito],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Escolha] | None:
    """Resolver do MRTR: decide se a reserva precisa perguntar algo ao cliente.

    O SDK roda este resolver antes do corpo da tool, em toda rodada:
    - devolver um valor comum (aqui, None) significa "não há pergunta";
    - devolver `Elicit(...)` faz o SDK responder `input_required` com essa
      elicitation em `inputRequests`, sob a chave "<módulo>:escolha_de_sala",
      e um `requestState` selado. Nada fica guardado em memória.
    No retry o resolver roda de novo, recalcula a mesma pergunta e o SDK só
    aceita a resposta se a pergunta for idêntica à que foi feita (digest).
    """
    ini, fi = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, ini, fi):
        return None
    opcoes = dominio.alternativas(sala, ini, fi)
    if not opcoes:
        # Sem alternativa não há elicitation: erro de execução com a mensagem exata.
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    # Schema plano gerado por pedido: `sala` restrita às alternativas, na ordem da
    # regra. Literal com 2+ valores vira `enum`; com um só, o pydantic emite `const`.
    schema = create_model(
        "Escolha",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", schema)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    # Parâmetro preenchido pelo resolver, não pelo cliente: não aparece no inputSchema.
    # Anotar com ElicitationResult faz o SDK entregar o desfecho completo
    # (accept/decline/cancel) em vez de abortar a chamada na recusa.
    escolha: Annotated[ElicitationResult[Escolha], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        # decline ou cancel: conclui normalmente, sem reservar e sem isError.
        return ReservaOut(reservado=False, motivo="recusado")
    # data None: o resolver não perguntou nada, a sala pedida está livre.
    # data preenchido: o cliente escolheu uma alternativa (já validada contra o enum).
    sala_final = sala if escolha.data is None else escolha.data.sala
    reserva = dominio.criar_reserva(sala_final, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva.id,
        sala=reserva.sala,
        inicio=reserva.inicio,
        fim=reserva.fim,
        responsavel=reserva.responsavel,
        politica=dominio.VERSAO_POLITICA,
    )


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------

@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica_de_uso() -> str:
    return dominio.TEXTO_POLITICA


# ---------------------------------------------------------------------------
# Log de cada request no stderr
# ---------------------------------------------------------------------------

def com_log(app: Any) -> Any:
    """Middleware ASGI que registra método, id e traceparent de cada request.

    Fica na borda HTTP, antes do SDK, para registrar inclusive os requests que
    o SDK rejeita (sem _meta, requestState adulterado, header divergente).
    O corpo é lido uma vez e reentregue intacto ao app.
    """

    async def wrapper(scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await app(scope, receive, send)
        corpo = b""
        while True:
            msg = await receive()
            corpo += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        try:
            dados = json.loads(corpo)
        except ValueError:
            dados = {}
        if isinstance(dados, dict):
            params = dados.get("params") if isinstance(dados.get("params"), dict) else {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            alvo = params.get("name") or params.get("uri")
            log(
                f"[mcp] method={dados.get('method')} id={json.dumps(dados.get('id'))}"
                + (f" name={alvo}" if alvo else "")
                + f" traceparent={meta.get('traceparent', '-')}"
                + (" retry=sim" if params.get("requestState") else "")
            )
        entregue = False

        async def replay():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await app(scope, replay, send)

    return wrapper


def main() -> None:
    # stateless_http: nenhuma sessão; json_response: uma resposta JSON por POST.
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=HOST)
    log(f"[mcp] central-de-salas ouvindo em http://{HOST}:{PORTA}/mcp")
    uvicorn.run(com_log(app), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
