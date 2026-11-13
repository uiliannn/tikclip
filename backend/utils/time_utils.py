"""Utilitários de tempo.

Motivo: o método ``utcnow`` do módulo ``datetime`` está deprecado a partir do
Python 3.12 e retornava um datetime *naive* (sem timezone). Trocar direto por
``datetime.now(timezone.utc)`` devolveria um datetime *aware*, e comparar um
aware com um naive levanta ``TypeError`` — o que quebraria comparações com
datas já gravadas no SQLite.

``utcnow()`` devolve o mesmo tipo de antes (naive, em UTC), apenas sem o
warning. Quando o banco migrar para timestamps com timezone, o único lugar a
mudar é aqui.
"""
from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utcnow", "utcnow_aware"]


def utcnow() -> datetime:
    """Agora, em UTC, como datetime naive (compatível com o código existente)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_aware() -> datetime:
    """Agora, em UTC, como datetime aware — para código novo."""
    return datetime.now(timezone.utc)
