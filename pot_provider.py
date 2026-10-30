"""Provedor de PO Token (bgutil) para o YouTube.

O que é: o YouTube passou a exigir um "PO Token" — uma atestação gerada por
JavaScript dentro do próprio YouTube — para liberar a API de player. Sem esse
token, qualquer IP marcado recebe "Sign in to confirm you're not a bot", e
cookie nenhum resolve.

Como foi montado (tudo em locais padrão, de propósito):
  plugin  -> %APPDATA%\\yt-dlp\\plugins\\bgutil-ytdlp-pot-provider
  servidor-> ~/bgutil-ytdlp-pot-provider/server  (build/generate_once.js)

Estando nos locais padrão, o yt-dlp encontra sozinho — então isto conserta
também o TikClip instalado, sem precisar alterar o código dele.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

NODE_MINIMO = (22, 0, 0)


def plugin_dir() -> Path:
    return Path(os.getenv('APPDATA', str(Path.home()))) / 'yt-dlp' / 'plugins' / 'bgutil-ytdlp-pot-provider'


def server_home() -> Path:
    return Path(os.path.expanduser('~')) / 'bgutil-ytdlp-pot-provider' / 'server'


def script_path() -> Path:
    return server_home() / 'build' / 'generate_once.js'


def _node_exe() -> str | None:
    return shutil.which('node')


def _node_versao() -> tuple[int, ...] | None:
    exe = _node_exe()
    if not exe:
        return None
    try:
        saida = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return None
    casamento = re.match(r'v(\d+)\.(\d+)\.(\d+)', saida)
    return tuple(int(parte) for parte in casamento.groups()) if casamento else None


def status() -> dict[str, Any]:
    versao = _node_versao()
    tem_plugin = (plugin_dir() / 'yt_dlp_plugins').is_dir()
    tem_build = script_path().is_file()

    if not _node_exe():
        situacao = 'sem-node'
        explicacao = 'O Node.js não foi encontrado. Sem ele o token não pode ser gerado.'
    elif not versao or versao < NODE_MINIMO:
        situacao = 'node-antigo'
        explicacao = f'O Node.js precisa ser {NODE_MINIMO[0]} ou mais novo.'
    elif not tem_plugin:
        situacao = 'sem-plugin'
        explicacao = 'O plugin do yt-dlp não está instalado.'
    elif not tem_build:
        situacao = 'sem-servidor'
        explicacao = 'O servidor do provedor não está compilado.'
    else:
        situacao = 'pronto'
        explicacao = 'Provedor ativo: o YouTube pode emitir o PO Token.'

    return {
        'situacao': situacao,
        'explicacao': explicacao,
        'node': '.'.join(map(str, versao)) if versao else None,
        'plugin_instalado': tem_plugin,
        'servidor_compilado': tem_build,
        'plugin_dir': str(plugin_dir()),
        'script': str(script_path()),
        'pronto': situacao == 'pronto',
    }


def extractor_args() -> dict[str, dict[str, str]]:
    """Argumentos extras, só quando o servidor não está no local padrão."""
    padrao = Path(os.path.expanduser('~')) / 'bgutil-ytdlp-pot-provider' / 'server'
    if server_home().resolve() == padrao.resolve():
        return {}
    return {'youtubepot-bgutilscript': {'server_home': str(server_home())}}


def testar_geracao() -> dict[str, Any]:
    """Roda o script direto, fora do yt-dlp, para ver se ele responde."""
    if not script_path().is_file():
        return {'ok': False, 'erro': 'O script do provedor não existe ainda.'}
    exe = _node_exe()
    if not exe:
        return {'ok': False, 'erro': 'Node.js não encontrado.'}
    try:
        proc = subprocess.run(
            [exe, str(script_path()), '--version'],
            capture_output=True, text=True, timeout=60,
            cwd=str(server_home()),
        )
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'erro': f'Falha ao executar: {exc}'}
    if proc.returncode != 0:
        return {
            'ok': False,
            'erro': ((proc.stderr or proc.stdout) or '').strip()[-400:] or f'código {proc.returncode}',
        }
    return {'ok': True, 'versao': (proc.stdout or '').strip()}
