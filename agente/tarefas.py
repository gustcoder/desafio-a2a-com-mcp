"""A Task do A2A v1.0: identidade, estado e produto.

Este módulo não sabe de MCP. Ele guarda as Tasks em memória e impõe a máquina
de estados do enunciado, para que uma transição ilegal (por exemplo, sair de
um estado terminal) seja impossível em vez de só "não acontecer".
"""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"

TERMINAIS = frozenset({COMPLETED, FAILED, CANCELED})

# Transições permitidas. Estados terminais não têm saída: são definitivos.
TRANSICOES: dict[str, frozenset[str]] = {
    SUBMITTED: frozenset({WORKING}),
    WORKING: frozenset({COMPLETED, FAILED, INPUT_REQUIRED}),
    # INPUT_REQUIRED -> INPUT_REQUIRED: escolha fora do enum, repete a pergunta.
    # INPUT_REQUIRED -> CANCELED: escolha=recusar.
    # INPUT_REQUIRED -> FAILED: a recusa não pôde ser entregue ao servidor MCP.
    INPUT_REQUIRED: frozenset({INPUT_REQUIRED, WORKING, CANCELED, FAILED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELED: frozenset(),
}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


class TransicaoInvalida(Exception):
    pass


@dataclass
class Tarefa:
    id: str
    context_id: str
    estado: str = SUBMITTED
    mensagem_status: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def registrar_usuario(self, mensagem: dict[str, Any]) -> None:
        """Mensagem do cliente entra no histórico como foi recebida."""
        self.historico.append(mensagem)

    def mudar(self, estado: str, texto: str | None = None) -> None:
        """Transita de estado; com `texto`, publica uma mensagem do agente no status e no histórico."""
        if estado not in TRANSICOES[self.estado]:
            raise TransicaoInvalida(f"{self.id}: {self.estado} -> {estado} nao e permitido")
        print(f"[a2a] {self.id} {self.estado} -> {estado}", file=sys.stderr, flush=True)
        self.estado = estado
        if texto is not None:
            mensagem = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.mensagem_status = mensagem
            self.historico.append(mensagem)

    def anexar_artifact(self, nome: str, texto: str) -> None:
        self.artifacts.append({"artifactId": novo_id("art"), "name": nome, "parts": [{"text": texto}]})

    def para_wire(self, history_length: int | None = None) -> dict[str, Any]:
        """A forma v1.0 da Task. Só o que está neste objeto sai: o requestState
        nunca é guardado aqui, então não tem como vazar por este caminho."""
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        historico = self.historico
        if history_length is not None:
            historico = historico[-history_length:] if history_length > 0 else []
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": list(historico),
            "artifacts": list(self.artifacts),
        }


class RegistroDeTarefas:
    """Tasks em memória, por id. Não sobrevivem a restart (fora de escopo)."""

    def __init__(self) -> None:
        self._tarefas: dict[str, Tarefa] = {}

    def criar(self, context_id: str | None = None) -> Tarefa:
        tarefa = Tarefa(id=novo_id("task"), context_id=context_id or novo_id("ctx"))
        self._tarefas[tarefa.id] = tarefa
        print(f"[a2a] {tarefa.id} criada em {SUBMITTED}", file=sys.stderr, flush=True)
        return tarefa

    def obter(self, task_id: str) -> Tarefa | None:
        return self._tarefas.get(task_id)
