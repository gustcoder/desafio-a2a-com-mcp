"""Demonstração do agente como host MCP puro, ainda sem A2A.

    uv run python demo_host_mcp.py sala-garagem 2026-11-03T14:00:00-03:00 2026-11-03T15:00:00-03:00 Marty

Descobre as tools, lê a política e chama `reservar_sala`. Se o servidor
responder `input_required`, mostra as alternativas e pergunta no terminal
(ou usa --escolha); depois faz o retry com `inputResponses` e o `requestState`.
Aqui quem responde a elicitation é você, à mão. Na Fase 3 quem responde é o
cliente A2A.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from mcp_types import ElicitResult

from cliente_mcp import ClienteMCP, Concluido, FalhaDaTool, PedeEntrada, Trace, montar_argumentos

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
TOOL = "reservar_sala"


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sala")
    p.add_argument("inicio")
    p.add_argument("fim")
    p.add_argument("responsavel")
    p.add_argument("--escolha", help="responde a elicitation sem perguntar (id da sala ou 'recusar')")
    p.add_argument("--traceparent", help="simula o header traceparent vindo de um cliente A2A")
    args = p.parse_args()

    trace = Trace.de_header(args.traceparent)
    print(f"trace-id: {trace.trace_id}")

    async with ClienteMCP(MCP_URL) as mcp:
        tools = await mcp.descobrir_tools(trace)
        print(f"1. tools/list -> {sorted(tools)}")
        if TOOL not in tools:
            raise SystemExit(f"o servidor nao oferece {TOOL}")

        versao = await mcp.ler_versao_politica(trace)
        print(f"2. resources/read politica://uso -> versao {versao}")

        argumentos = montar_argumentos(
            tools[TOOL], {"sala": args.sala, "inicio": args.inicio, "fim": args.fim, "responsavel": args.responsavel}
        )
        resultado = await mcp.chamar_tool(TOOL, argumentos, trace)
        print(f"3. tools/call {TOOL} -> {type(resultado).__name__}")

        if isinstance(resultado, PedeEntrada):
            print(f"   mensagem: {resultado.mensagem}")
            print(f"   alternativas: {', '.join(resultado.opcoes)}")
            print(f"   chave: {resultado.chave}   requestState: {resultado.request_state[:24]}... (opaco)")
            escolha = args.escolha or input("   escolha (id ou 'recusar'): ").strip()
            if escolha == "recusar":
                resposta = ElicitResult(action="decline")
            else:
                resposta = ElicitResult(action="accept", content={resultado.campo: escolha})
            # O retry: mesma tool, mesmos argumentos, id novo (o SDK gera),
            # resposta sob a MESMA chave e o requestState ecoado sem modificação.
            resultado = await mcp.chamar_tool(
                TOOL,
                argumentos,
                trace,
                input_responses={resultado.chave: resposta},
                request_state=resultado.request_state,
            )
            print(f"4. retry tools/call {TOOL} -> {type(resultado).__name__}")

        if isinstance(resultado, Concluido):
            dados = dict(resultado.dados or {})
            if dados.get("reservado"):
                dados["politica"] = versao  # a versão vem do resource lido pelo agente
            print(json.dumps(dados, ensure_ascii=False, indent=2))
        elif isinstance(resultado, FalhaDaTool):
            print(f"   erro da tool: {resultado.mensagem}")
        else:
            print(f"   ainda pede entrada: alternativas {resultado.opcoes}")


if __name__ == "__main__":
    asyncio.run(main())
