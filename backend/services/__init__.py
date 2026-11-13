"""
Business logic services layer.
Separates business logic from API controllers and data access.

Os símbolos abaixo são importados sob demanda (PEP 562). Antes, este
``__init__`` importava ``.base`` de forma ansiosa, o que arrastava SQLAlchemy
para qualquer import de submódulo — inclusive módulos puros, sem banco, como
``backend.services.subtitle_timing``. O efeito prático era não conseguir
testar nada de ``backend.services`` sem instalar a stack inteira.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "BaseService",
    "ServiceError",
]


def __getattr__(name: str) -> Any:
    if name == "BaseService":
        from .base import BaseService

        return BaseService
    if name == "ServiceError":
        from .exceptions import ServiceError

        return ServiceError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
