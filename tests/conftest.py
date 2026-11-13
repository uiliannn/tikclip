"""Bootstrap dos testes do TikClip.

Garante que a raiz do repositório esteja no ``sys.path`` para que
``backend.services.*`` e os módulos de topo (``video_translator``) possam ser
importados sem instalar o projeto.
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))
