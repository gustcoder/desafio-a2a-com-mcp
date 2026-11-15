"""O lado de dentro do agente: um host MCP que fala HTTP com o servidor de salas.

Este módulo é só protocolo MCP. Ele não sabe de A2A nem de regra de sala:
descobre tools, lê resource, chama tool e devolve o resultado normalizado em
três formas (Concluido, FalhaDaTool, PedeEntrada). Quem decide o que fazer
com cada forma é a ponte (Fase 3).
"""

from __future__ import annotations

import re
import secrets
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import Client
from mcp_types import (
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    ElicitResult,
    FormElicitationCapability,
    Implementation,
    InputRequiredResult,
    Tool,
)

PROTOCOLO = "2026-07-28"
INFO_CLIENTE = Implementation(name="agente-central-de-salas", version="1.0.0")

# Exatamente o que a spec pede, e nada além: elicitation em form mode.
CAPABILITIES = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))


# ---------------------------------------------------------------------------
# Trace context (W3C traceparent)
# ---------------------------------------------------------------------------

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True)
class Trace:
    """O trace de uma Task. O trace-id é fixo; cada request MCP ganha um span-id novo."""

    trace_id: str
    flags: str = "01"

    @classmethod
    def de_header(cls, header: str | None) -> Trace:
        """Herda o trace-id do traceparent do cliente A2A; sem header válido, abre um trace novo."""
        m = _TRACEPARENT.match((header or "").strip().lower())
        if m and m.group(1) != "ff" and m.group(2) != "0" * 32 and m.group(3) != "0" * 16:
            return cls(trace_id=m.group(2), flags=m.group(4))
        return cls(trace_id=secrets.token_hex(16))

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"

    def meta(self) -> dict[str, Any]:
        """Entra no `_meta` do request. O SDK acrescenta protocolVersion, clientInfo e capabilities."""
        return {"traceparent": self.traceparent()}


# ---------------------------------------------------------------------------
# Resultados normalizados de um tools/call
# ---------------------------------------------------------------------------

@dataclass
class Concluido:
    """`complete` sem isError: a tool terminou."""

    dados: dict[str, Any] | None  # structuredContent
    texto: str


@dataclass
class FalhaDaTool:
    """`complete` com isError: true. A mensagem é a da tool, sem retoque."""

    mensagem: str


@dataclass
class PedeEntrada:
    """`input_required` com uma elicitation em form mode.

    `request_state` é opaco: guardar e ecoar, nunca abrir nem interpretar.
    `campo` e `opcoes` saem do `requestedSchema` que o servidor mandou, não de
    conhecimento do domínio: o agente só lê o schema.
    """

    chave: str
    mensagem: str
    campo: str
    opcoes: list[str]
    request_state: str


class ErroDeProtocolo(Exception):
    """Algo no protocolo que o agente não sabe atender (ex.: input_required sem form)."""


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

class _ClienteSoForm(Client):
    """`Client` do SDK declarando só elicitation em form mode, e sem callback.

    Limitação do SDK 2.3.0 (mcp/client/session.py, `_build_capabilities`): a
    capability de elicitation só é declarada se houver `elicitation_callback`,
    e aí ela sai como `{form, url}`. Um callback faria o SDK responder a
    elicitation sozinho, e a Task A2A nunca pausaria. Então substituímos apenas
    a construção das capabilities da sessão; nenhum callback é registrado e o
    `input_required` chega cru até o agente.
    """

    async def _build_session(self, exit_stack: AsyncExitStack):  # type: ignore[override]
        sessao = await super()._build_session(exit_stack)
        sessao._build_capabilities = lambda _versao: CAPABILITIES  # type: ignore[method-assign]
        return sessao


class ClienteMCP:
    """Conexão de vida longa com o servidor MCP.

    Manter o objeto vivo é só reaproveitamento de conexão HTTP: cada request
    carrega no `_meta` a versão e as capabilities (o protocolo não tem sessão).
    """

    def __init__(self, url: str) -> None:
        self.url = url
        # mode fixo na revisão 2026-07-28: sem handshake e sem probe de versão.
        self._cliente = _ClienteSoForm(url, mode=PROTOCOLO, client_info=INFO_CLIENTE)

    async def __aenter__(self) -> ClienteMCP:
        await self._cliente.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._cliente.__aexit__(*exc)

    async def descobrir_tools(self, trace: Trace) -> dict[str, Tool]:
        """tools/list: o agente não carrega lista fixa, pergunta ao servidor."""
        resultado = await self._cliente.list_tools(meta=trace.meta())
        return {t.name: t for t in resultado.tools}

    async def ler_versao_politica(self, trace: Trace, uri: str = "politica://uso") -> str:
        """resources/read: a aplicação escolhe ler o resource; a versão é a 1ª linha."""
        resultado = await self._cliente.read_resource(uri, meta=trace.meta())
        texto = next((c.text for c in resultado.contents if hasattr(c, "text")), "")
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        if chave.strip() != "versao" or not valor.strip():
            raise ErroDeProtocolo(f"resource {uri} sem a linha de versao")
        return valor.strip()

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace: Trace,
        *,
        input_responses: dict[str, ElicitResult] | None = None,
        request_state: str | None = None,
    ) -> Concluido | FalhaDaTool | PedeEntrada:
        """tools/call, sem resolver nada por conta própria.

        `allow_input_required=True` faz o SDK devolver o `InputRequiredResult`
        em vez de tentar respondê-lo. No retry, passe `input_responses` e o
        `request_state` recebido, intacto. O SDK gera um id de JSON-RPC novo a
        cada chamada, então o retry nunca reaproveita o id do request inicial.
        """
        resultado = await self._cliente.session.call_tool(
            nome,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=trace.meta(),
            allow_input_required=True,
        )
        if isinstance(resultado, InputRequiredResult):
            return _pede_entrada(resultado)
        if isinstance(resultado, CallToolResult):
            texto = " ".join(getattr(b, "text", "") for b in resultado.content).strip()
            if resultado.is_error:
                return FalhaDaTool(mensagem=texto)
            return Concluido(dados=resultado.structured_content, texto=texto)
        raise ErroDeProtocolo(f"resultado inesperado de tools/call: {type(resultado).__name__}")


def _pede_entrada(resultado: InputRequiredResult) -> PedeEntrada:
    """Lê o pedido de input sem interpretar o domínio: só o schema da elicitation."""
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1 or not resultado.request_state:
        raise ErroDeProtocolo(f"input_required com {len(pedidos)} pedidos; o agente atende exatamente um")
    chave, pedido = next(iter(pedidos.items()))
    params = getattr(pedido, "params", None)
    if getattr(pedido, "method", None) != "elicitation/create" or getattr(params, "mode", None) != "form":
        raise ErroDeProtocolo("input_required que nao e elicitation em form mode")
    propriedades = (params.requested_schema or {}).get("properties") or {}
    if len(propriedades) != 1:
        raise ErroDeProtocolo("requestedSchema com mais de um campo")
    campo, definicao = next(iter(propriedades.items()))
    # Duas ou mais opções chegam como enum; uma só pode chegar como const.
    opcoes = definicao.get("enum") or ([definicao["const"]] if "const" in definicao else [])
    if not opcoes:
        raise ErroDeProtocolo("requestedSchema sem enum nem const")
    return PedeEntrada(
        chave=chave,
        mensagem=params.message,
        campo=campo,
        opcoes=[str(o) for o in opcoes],
        request_state=resultado.request_state,
    )


def montar_argumentos(tool: Tool, campos: dict[str, str]) -> dict[str, Any]:
    """Casa os campos do pedido com o inputSchema descoberto em runtime.

    Envia só as propriedades que a tool declara e falha se faltar um campo
    obrigatório, em vez de confiar num formato fixo no código.
    """
    schema = tool.input_schema or {}
    propriedades = schema.get("properties") or {}
    faltando = [c for c in schema.get("required", []) if c not in campos]
    if faltando:
        raise ValueError(f"faltando no pedido: {', '.join(faltando)}")
    return {k: v for k, v in campos.items() if k in propriedades}
