"""A ponte: onde o estado do MCP (requestState) encontra o estado do A2A (Task).

Os dois protocolos não têm sessão, e cada um nomeia o próprio estado:
- o MCP devolve um `requestState` opaco e espera recebê-lo de volta no retry;
- o A2A tem a Task, com identidade, estado e produto.

Quando o servidor MCP responde `input_required`, o agente não responde por
conta própria nem fica esperando. Ele:
  1. interrompe a Task (TASK_STATE_INPUT_REQUIRED);
  2. devolve a pergunta ao cliente A2A ("alternativas: ...");
  3. guarda o requestState ligado àquela Task, fora do objeto Task;
  4. quando a resposta chega, repete o tools/call original com um id novo.

O agente traduz protocolo, não domínio: conflito, política e alternativas são
decididos pelo servidor MCP. Aqui só se lê o que o protocolo entrega.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from mcp_types import ElicitResult

from cliente_mcp import (
    ArgumentosIncompletos,
    ClienteMCP,
    Concluido,
    ErroDeProtocolo,
    FalhaDaTool,
    PedeEntrada,
    Trace,
    montar_argumentos,
)
from tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, TRANSICOES, WORKING, Tarefa

# A skill `reservar-sala` do card é atendida por esta tool do servidor MCP.
# O agente confere em runtime (tools/list) que ela existe e qual é o schema.
TOOL_RESERVA = "reservar_sala"
RECUSAR = "recusar"
USO = "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"

# key=valor; o valor vai até o próximo " chave=" (permite espaço no nome).
_CAMPO = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)")
_ESCOLHA = re.compile(r"^escolha=(\S+)$")


class PedidoInvalido(Exception):
    pass


@dataclass(frozen=True)
class Contexto:
    """O tools/call original de uma Task: o que o retry precisa repetir igual."""

    trace: Trace
    tool: str
    argumentos: dict[str, Any]
    versao_politica: str


@dataclass(frozen=True)
class Pausa:
    """Uma Task pausada: o tools/call original e o pedido de input do servidor.

    `pedido.request_state` é guardado e ecoado sem modificação: o agente nunca
    abre, interpreta nem reconstrói esse valor.
    """

    contexto: Contexto
    pedido: PedeEntrada


def interpretar_pedido(texto: str) -> dict[str, str]:
    """Formato fixo: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>."""
    verbo, _, resto = texto.strip().partition(" ")
    campos = {k: v.strip() for k, v in _CAMPO.findall(resto)}
    if verbo != "reservar" or not campos:
        raise PedidoInvalido(USO)
    return campos


def interpretar_escolha(texto: str) -> str | None:
    m = _ESCOLHA.match(texto.strip())
    return m.group(1) if m else None


def linha_alternativas(opcoes: list[str]) -> str:
    """Exatamente esta linha, sem prefixo nem saudação: o avaliador compara byte a byte."""
    return "alternativas: " + ", ".join(opcoes)


class Ponte:
    def __init__(self, mcp: ClienteMCP) -> None:
        self._mcp = mcp
        # requestState e companhia, por Task. Separado de Tarefa de propósito:
        # o que não está na Tarefa não tem como sair em nenhuma resposta A2A.
        self._pausas: dict[str, Pausa] = {}

    async def iniciar(self, tarefa: Tarefa, texto: str, trace: Trace) -> None:
        """Primeiro SendMessage: descobre, lê a política e faz o tools/call inicial."""
        tarefa.mudar(WORKING)
        try:
            campos = interpretar_pedido(texto)
            tools = await self._mcp.descobrir_tools(trace)
            if TOOL_RESERVA not in tools:
                raise ErroDeProtocolo(f"o servidor nao oferece a tool {TOOL_RESERVA}")
            argumentos = montar_argumentos(tools[TOOL_RESERVA], campos)
            versao = await self._mcp.ler_versao_politica(trace)
            contexto = Contexto(trace, TOOL_RESERVA, argumentos, versao)
            resultado = await self._mcp.chamar_tool(contexto.tool, contexto.argumentos, trace)
        except PedidoInvalido as e:
            tarefa.mudar(FAILED, str(e))
            return
        except ArgumentosIncompletos as e:
            tarefa.mudar(FAILED, f"{USO} ({e})")
            return
        except Exception as e:  # servidor MCP fora do ar, erro de protocolo...
            tarefa.mudar(FAILED, _falha_mcp(e))
            return
        self._aplicar(tarefa, resultado, contexto)

    async def continuar(self, tarefa: Tarefa, texto: str) -> None:
        """SendMessage de continuação, com a Task em TASK_STATE_INPUT_REQUIRED."""
        pausa = self._pausas[tarefa.id]
        contexto, pedido = pausa.contexto, pausa.pedido
        escolha = interpretar_escolha(texto)

        # Escolha malformada ou fora do enum: a Task continua pausada e repete
        # a pergunta. Não é regra de sala, é o schema que o próprio servidor
        # mandou; repassar um valor fora dele faria a tool falhar e a Task iria
        # a FAILED em vez de continuar esperando.
        if escolha is None or (escolha != RECUSAR and escolha not in pedido.opcoes):
            tarefa.mudar(INPUT_REQUIRED, linha_alternativas(pedido.opcoes))
            return

        if escolha == RECUSAR:
            # Recusa vira action=decline na elicitation. Quem decide o desfecho
            # ainda é o servidor MCP; a Task vai direto a CANCELED (diagrama).
            resposta = ElicitResult(action="decline")
        else:
            tarefa.mudar(WORKING)
            resposta = ElicitResult(action="accept", content={pedido.campo: escolha})

        try:
            # O retry: mesma tool e mesmos argumentos, id de JSON-RPC novo
            # (gerado pelo SDK), resposta sob a MESMA chave do inputRequests e
            # o requestState ecoado exatamente como chegou.
            resultado = await self._mcp.chamar_tool(
                contexto.tool,
                contexto.argumentos,
                contexto.trace,
                input_responses={pedido.chave: resposta},
                request_state=pedido.request_state,
            )
        except Exception as e:
            self._pausas.pop(tarefa.id, None)
            self._encerrar(tarefa, FAILED, _falha_mcp(e, pedido.request_state))
            return
        self._aplicar(tarefa, resultado, contexto)

    def _aplicar(self, tarefa: Tarefa, resultado: Concluido | FalhaDaTool | PedeEntrada, contexto: Contexto) -> None:
        """Traduz o resultado MCP em estado da Task."""
        if isinstance(resultado, PedeEntrada):
            # >>> A PONTE: input_required do MCP vira TASK_STATE_INPUT_REQUIRED.
            self._pausas[tarefa.id] = Pausa(contexto, resultado)
            tarefa.mudar(INPUT_REQUIRED, linha_alternativas(resultado.opcoes))
            return

        self._pausas.pop(tarefa.id, None)
        if isinstance(resultado, FalhaDaTool):
            # Erro de execução da tool: a mensagem exata vai para o histórico.
            self._encerrar(tarefa, FAILED, resultado.mensagem)
            return

        dados = resultado.dados or {}
        if not dados.get("reservado"):
            motivo = dados.get("motivo") or "nao reservado"
            self._encerrar(tarefa, CANCELED, f"Reserva nao realizada: {motivo}.")
            return

        reserva = {k: dados.get(k) for k in ("reserva", "sala", "inicio", "fim", "responsavel")}
        reserva["politica"] = contexto.versao_politica  # versão lida do resource pelo próprio agente
        tarefa.anexar_artifact("reserva", json.dumps(reserva, ensure_ascii=False))
        self._encerrar(tarefa, COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    @staticmethod
    def _encerrar(tarefa: Tarefa, estado: str, texto: str) -> None:
        # Se a Task ainda estiver pausada (caso da recusa) e o desfecho não for
        # alcançável direto de INPUT_REQUIRED, passa por WORKING antes.
        if estado not in TRANSICOES[tarefa.estado]:
            tarefa.mudar(WORKING)
        tarefa.mudar(estado, texto)


def _falha_mcp(erro: Exception, request_state: str | None = None) -> str:
    texto = f"Falha ao falar com o servidor MCP: {erro}"
    if request_state:
        # Nunca devolver o requestState ao cliente A2A, nem por acidente.
        texto = texto.replace(request_state, "<requestState>")
    return texto
