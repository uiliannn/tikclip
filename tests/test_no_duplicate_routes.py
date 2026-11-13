"""Regressão: rotas HTTP duplicadas.

O bug original era `POST /paths/data-directory` definido duas vezes em
`backend/api/v1/settings.py`. O FastAPI resolve rotas na ordem de registro,
então o primeiro handler vencia silenciosamente e o segundo virava código
morto — quem chamasse com corpo JSON recebia 422.

Este teste lê o código com AST (sem importar FastAPI nem o banco), então roda
em qualquer ambiente.
"""
from __future__ import annotations

import ast
import collections
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]

FONTES = sorted((RAIZ / "backend" / "api" / "v1").glob("*.py")) + [
    RAIZ / "api_ext.py",
    RAIZ / "supo_api.py",
]

METODOS = {"get", "post", "put", "patch", "delete", "head", "options"}


def rotas_do_arquivo(caminho: pathlib.Path) -> list[tuple[str, str, str, int]]:
    """Extrai (MÉTODO, caminho, função, linha) dos decoradores @router.*."""
    arvore = ast.parse(caminho.read_text(encoding="utf-8", errors="ignore"))
    achados: list[tuple[str, str, str, int]] = []

    for node in ast.walk(arvore):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorador in node.decorator_list:
            if not isinstance(decorador, ast.Call):
                continue
            func = decorador.func
            if not isinstance(func, ast.Attribute) or func.attr.lower() not in METODOS:
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "router":
                continue
            if not decorador.args:
                continue
            primeiro = decorador.args[0]
            if isinstance(primeiro, ast.Constant) and isinstance(primeiro.value, str):
                achados.append((func.attr.upper(), primeiro.value, node.name, node.lineno))
    return achados


def test_sem_rotas_duplicadas() -> None:
    problemas: list[str] = []
    total = 0

    for caminho in FONTES:
        if not caminho.is_file():
            continue
        vistas: dict[tuple[str, str], list[tuple[int, str]]] = collections.defaultdict(list)
        for metodo, rota, funcao, linha in rotas_do_arquivo(caminho):
            total += 1
            vistas[(metodo, rota)].append((linha, funcao))

        for (metodo, rota), ocorrencias in vistas.items():
            if len(ocorrencias) > 1:
                detalhe = ", ".join(f"linha {l} ({f})" for l, f in ocorrencias)
                problemas.append(f"{caminho.name}: {metodo} {rota} -> {detalhe}")

    assert total > 0, "nenhuma rota encontrada: o parser ou os caminhos mudaram"
    assert not problemas, "rotas duplicadas (a primeira vence silenciosamente):\n" + "\n".join(problemas)


def test_caminhos_comecam_com_barra() -> None:
    fora: list[str] = []
    for caminho in FONTES:
        if not caminho.is_file():
            continue
        for metodo, rota, funcao, linha in rotas_do_arquivo(caminho):
            if not rota.startswith("/"):
                fora.append(f"{caminho.name}:{linha} {metodo} {rota!r} ({funcao})")
    assert not fora, "rotas sem barra inicial:\n" + "\n".join(fora)
