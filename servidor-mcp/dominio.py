"""Regras de domínio da Central de Salas.

Este módulo não sabe nada de MCP: são funções puras sobre os dados de
`dados/`. O servidor (servidor.py) traduz o resultado delas para o protocolo.
Mantê-las separadas deixa claro onde termina o domínio e começa a fronteira.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"

# Horário de São Paulo, como a política declara. Fixo em -03:00 (sem horário de verão).
FUSO_SP = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

# Mensagens exatas exigidas pelo enunciado (fonte única de verdade).
ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
ERRO_DATA = "Data invalida: {valor}"


class ErroDeRegra(Exception):
    """Violação de regra de negócio. Vira `isError: true` na borda MCP."""


@dataclass
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str

    @property
    def intervalo(self) -> tuple[datetime, datetime]:
        return _parse(self.inicio), _parse(self.fim)


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: dict[str, dict] = {s["id"]: s for s in _ler_json("salas.json")}
TEXTO_POLITICA: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
# Primeira linha: "versao: 2026-11-01"
VERSAO_POLITICA: str = TEXTO_POLITICA.splitlines()[0].split(":", 1)[1].strip()

# Persistência em memória: começa com as reservas do starter e cresce durante o
# processo. Não sobrevive a restart, e o enunciado diz que não precisa.
RESERVAS: list[Reserva] = [Reserva(**r) for r in _ler_json("reservas.json")]


def _parse(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise ErroDeRegra(ERRO_DATA.format(valor=valor)) from None
    # Sem offset explícito, assume o horário da política.
    return dt if dt.tzinfo else dt.replace(tzinfo=FUSO_SP)


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as validações de sala e de política, na ordem do enunciado.

    Levanta `ErroDeRegra` com a mensagem exata; devolve o intervalo já parseado.
    """
    if sala not in SALAS:
        raise ErroDeRegra(ERRO_SALA.format(sala=sala))
    ini, fi = _parse(inicio), _parse(fim)
    if fi <= ini:
        raise ErroDeRegra(ERRO_INTERVALO)
    ini_sp, fim_sp = ini.astimezone(FUSO_SP), fi.astimezone(FUSO_SP)
    abre = datetime.combine(ini_sp.date(), JANELA_INICIO, FUSO_SP)
    fecha = datetime.combine(ini_sp.date(), JANELA_FIM, FUSO_SP)
    if ini_sp < abre or fim_sp > fecha:
        raise ErroDeRegra(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ErroDeRegra(ERRO_DURACAO)
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[Reserva]:
    """Reservas da sala que se sobrepõem a [ini, fi). Intervalos encostados não conflitam."""
    resultado = []
    for r in RESERVAS:
        if r.sala != sala:
            continue
        r_ini, r_fim = r.intervalo
        if ini < r_fim and r_ini < fi:
            resultado.append(r)
    return resultado


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    """Regra fechada do enunciado: salas livres no intervalo, com capacidade >= a
    da pedida, no máximo três, por capacidade crescente e depois por id."""
    capacidade = SALAS[sala]["capacidade"]
    candidatas = [
        s for s in SALAS.values()
        if s["id"] != sala and s["capacidade"] >= capacidade and not conflitos(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> Reserva:
    reserva = Reserva(
        id=f"res-{len(RESERVAS) + 1:04d}", sala=sala, inicio=inicio, fim=fim, responsavel=responsavel
    )
    RESERVAS.append(reserva)
    return reserva
