"""Login do YouTube dentro do app, gerando cookies para o yt-dlp.

Por que existe: o yt-dlp é barrado com "Sign in to confirm you're not a bot"
quando o IP/sessão não tem cookies válidos. A correção é um login real, em um
navegador com perfil persistente, e exportar esses cookies no formato Netscape.

O arquivo é gravado em `<dados>/yt_cookies.txt`. O TikClip (versão instalada)
procura `*cookies*.txt` / `*youtube*.txt` em `%APPDATA%\\TikClip` e escolhe o
mais recente — então o login feito aqui também resolve o erro no app antigo,
sem precisar mexer na interface dele.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from backend.services import tiktok_local_publisher as ttl

# cookies que só existem depois de autenticar de verdade (checagem rígida no login)
COOKIES_DE_LOGIN = ('SAPISID', '__Secure-3PSID')
# conjunto mais amplo, usado no diagnóstico de arquivos já exportados
COOKIES_AUTENTICADO = ('SAPISID', '__Secure-3PSID', '__Secure-1PSID', 'SID', 'LOGIN_INFO')


def data_dir() -> Path:
    raw = os.getenv('TIKCLIP_DATA_DIR')
    return Path(raw) if raw else Path(os.getenv('APPDATA', str(Path.home()))) / 'TikClip'


def cookie_path() -> Path:
    return data_dir() / 'yt_cookies.txt'


def profile_dir() -> Path:
    return data_dir() / 'youtube-profile'


def _garantir_playwright() -> None:
    """Reusa o runtime do publicador (mesma pasta, sem baixar de novo)."""
    try:
        ttl._ensure_runtime()  # noqa: SLF001 - runtime compartilhado de propósito
        return
    except Exception:
        pass

    alvo = str(data_dir() / 'tiktok-runtime')
    if alvo not in sys.path:
        sys.path.insert(0, alvo)
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            'Playwright não está disponível. Abra o publicador do TikTok uma vez '
            'para instalar o runtime, ou reinstale o app.'
        ) from exc


def _navavegador_canal() -> str:
    try:
        return ttl._channel()  # noqa: SLF001
    except Exception:
        return 'msedge'


def esta_logado(cookies: list[dict[str, Any]]) -> bool:
    nomes = {c.get('name') for c in cookies}
    return all(nome in nomes for nome in COOKIES_DE_LOGIN)


def _nats_cape(cookies: list[dict[str, Any]]) -> str:
    """Serializa no formato Netscape, aceito pelo yt-dlp e pelo MozillaCookieJar."""
    linhas = [
        '# Netscape HTTP Cookie File',
        f'# Gerado pelo TikClip em {time.strftime("%d/%m/%Y %H:%M")}',
        '# Este arquivo dá acesso à sua conta do YouTube. Não compartilhe.',
        '',
    ]
    for cookie in cookies:
        dominio = (cookie.get('domain') or '').strip()
        if not dominio:
            continue
        nome = (cookie.get('name') or '').strip()
        valor = (cookie.get('value') or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')
        if not nome:
            continue

        expira = cookie.get('expires') or 0
        try:
            expira = int(expira)
        except (TypeError, ValueError):
            expira = 0
        if expira < 0:
            expira = 0

        campo_dominio = dominio
        if cookie.get('httpOnly'):
            # convenção do formato: cookies HttpOnly levam este prefixo, e os
            # cookies de login do Google são justamente HttpOnly
            campo_dominio = '#HttpOnly_' + dominio

        linhas.append('\t'.join([
            campo_dominio,
            'TRUE' if dominio.startswith('.') else 'FALSE',
            cookie.get('path') or '/',
            'TRUE' if cookie.get('secure') else 'FALSE',
            str(expira),
            nome,
            valor,
        ]))
    return '\n'.join(linhas) + '\n'


def status() -> dict[str, Any]:
    alvo = cookie_path()
    perfil = profile_dir()
    dados: dict[str, Any] = {
        'cookie_file': str(alvo),
        'tem_cookies': alvo.is_file() and alvo.stat().st_size > 0,
        'tem_login': _tem_cookie_de_login(alvo),
        'idade_min': None,
        'tamanho': None,
        'perfil_existe': perfil.is_dir() and any(perfil.iterdir()) if perfil.is_dir() else False,
        'navegador': _navavegador_canal(),
        'candidatos': _candidatos(),
    }
    if dados['tem_cookies']:
        info = alvo.stat()
        dados['idade_min'] = round((time.time() - info.st_mtime) / 60, 1)
        dados['tamanho'] = info.st_size
    return dados


def _nomes_do_arquivo(caminho: Path) -> set[str]:
    nomes: set[str] = set()
    try:
        with caminho.open('r', encoding='utf-8', errors='replace') as fh:
            for linha in fh:
                if not linha.strip() or linha.lstrip().startswith('#'):
                    continue
                campos = linha.rstrip('\n').split('\t')
                if len(campos) >= 7:
                    nomes.add(campos[5])
    except Exception:
        pass
    return nomes


def _tem_cookie_de_login(caminho: Path) -> bool:
    """Só uma sessão autenticada tem estes; arquivo anônimo não resolve o bloqueio."""
    if not caminho.is_file():
        return False
    return bool(_nomes_do_arquivo(caminho) & set(COOKIES_AUTENTICADO))


def _candidatos() -> list[dict[str, Any]]:
    """Reproduz a busca do TikClip: ele usa o arquivo mais recente que casar."""
    pastas = [data_dir(), Path.home() / 'Downloads']
    vistos: set[Path] = set()
    achados: list[tuple[float, Path]] = []
    for pasta in pastas:
        if not pasta.is_dir():
            continue
        for padrao in ('*cookies*.txt', '*cookie*.txt', '*youtube*.txt'):
            for arq in pasta.glob(padrao):
                try:
                    resolvido = arq.resolve()
                    if resolvido in vistos or not arq.is_file() or arq.stat().st_size == 0:
                        continue
                    vistos.add(resolvido)
                    achados.append((arq.stat().st_mtime, arq))
                except Exception:
                    continue

    achados.sort(key=lambda par: par[0], reverse=True)
    lista = []
    for indice, (mtime, arq) in enumerate(achados):
        lista.append({
            'arquivo': str(arq),
            'nome': arq.name,
            'tamanho': arq.stat().st_size,
            'idade_min': round((time.time() - mtime) / 60, 1),
            'tem_login': _tem_cookie_de_login(arq),
            'seria_usado': indice == 0,
        })
    return lista


def login_e_exportar(timeout_s: int = 600, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Abre a janela para o login e, ao detectar a sessão, exporta os cookies."""
    aviso = progress or (lambda _msg: None)
    _garantir_playwright()

    from playwright.sync_api import sync_playwright

    perfil = profile_dir()
    perfil.mkdir(parents=True, exist_ok=True)
    aviso('Abrindo o YouTube para você entrar na sua conta...')

    exportados = 0
    entrou = False

    with sync_playwright() as p:
        contexto = p.chromium.launch_persistent_context(
            user_data_dir=str(perfil),
            channel=_navavegador_canal(),
            headless=False,
            no_viewport=True,
            args=[
                '--start-maximized',
                '--disable-blink-features=AutomationControlled',
                '--no-first-run',
                '--no-default-browser-check',
            ],
        )
        try:
            pagina = contexto.pages[0] if contexto.pages else contexto.new_page()
            try:
                pagina.goto('https://www.youtube.com', wait_until='domcontentloaded', timeout=90000)
            except Exception:
                aviso('A página demorou para abrir, mas você pode navegar na janela.')

            aviso('Faça o login na janela. Ela fecha sozinha quando a sessão for detectada.')

            limite = time.time() + timeout_s
            while time.time() < limite:
                try:
                    cookies = contexto.cookies()
                except Exception:
                    cookies = []
                if esta_logado(cookies):
                    entrou = True
                    time.sleep(2.5)  # deixa o Google gravar os últimos cookies
                    break
                time.sleep(2.0)

            cookies = contexto.cookies()
            if cookies:
                texto = _nats_cape(cookies)
                destino = cookie_path()
                destino.write_text(texto, encoding='utf-8')
                exportados = sum(1 for linha in texto.splitlines() if linha and not linha.startswith('#'))
        finally:
            try:
                contexto.close()
            except Exception:
                pass

    if not exportados:
        raise RuntimeError(
            'Nenhum cookie foi capturado. Feche a janela só depois de concluir o login no YouTube.'
        )

    aviso(f'{exportados} cookies exportados. O YouTube deve parar de pedir verificação.')
    return {
        'ok': True,
        'logado': entrou,
        'cookies': exportados,
        'arquivo': str(cookie_path()),
    }


def testar(timeout_s: int = 90) -> dict[str, Any]:
    """Tenta ler um vídeo público com os cookies para saber se passaram a valer."""
    alvo = cookie_path()
    if not alvo.is_file():
        return {'ok': False, 'erro': 'Ainda não há cookies. Clique em "Conectar YouTube".'}

    try:
        import yt_dlp
    except ImportError as exc:
        return {'ok': False, 'erro': f'yt-dlp indisponível: {exc}'}

    opcoes = {
        'cookiefile': str(alvo),
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': timeout_s,
        'extract_flat': 'in_playlist',
    }
    # vídeo público e estável, usado só como canário
    amostra = 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'
    try:
        with yt_dlp.YoutubeDL(opcoes) as ydl:
            info = ydl.extract_info(amostra, download=False)
        return {
            'ok': True,
            'titulo': (info or {}).get('title'),
            'canal': (info or {}).get('uploader'),
        }
    except Exception as exc:  # noqa: BLE001
        mensagem = str(exc)
        dica = ''
        if 'not a bot' in mensagem or 'Sign in to confirm' in mensagem:
            dica = ('O YouTube ainda bloqueou. Refaça o login e, na janela, confirme '
                    'que aparece sua foto de perfil no canto superior direito.')
        elif 'cookies' in mensagem.lower():
            dica = 'Os cookies parecem inválidos ou expirados. Refaça o login.'
        return {'ok': False, 'erro': mensagem[-600:], 'dica': dica}


AMOSTRA = 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'


def diagnostico(timeout_s: int = 180) -> dict[str, Any]:
    """Verifica a cadeia inteira e diz onde está falhando.

    Roda o yt-dlp pela linha de comando porque as mensagens de PO Token só
    aparecem no log verboso — é assim que o projeto upstream instrui a checar.
    """
    import pot_provider

    provedor = pot_provider.status()
    tem_sessao = _tem_cookie_de_login(cookie_path())
    etapas: list[dict[str, Any]] = []

    etapas.append({
        'nome': 'Provedor de PO Token',
        'ok': provedor['pronto'],
        'detalhe': provedor['explicacao'],
    })
    etapas.append({
        'nome': 'Sessão do YouTube nos cookies',
        'ok': tem_sessao,
        'detalhe': 'cookies com sessão' if tem_sessao else 'cookies anônimos (sem login)',
    })

    tokens: list[str] = []
    erro = ''
    codigo = -1
    if provedor['pronto'] and tem_sessao:
        import subprocess

        extra = ['--extractor-args', 'youtube:player_client=tv;fetch_pot=always']
        comando = [
            sys.executable, '-m', 'yt_dlp', '-v', '--skip-download',
            '--cookies', str(cookie_path()), *extra, AMOSTRA,
        ]
        try:
            proc = subprocess.run(
                comando, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=timeout_s,
            )
            codigo = proc.returncode
            saida = (proc.stdout or '') + (proc.stderr or '')
            tokens = [l.strip() for l in saida.splitlines() if 'Generating a' in l and 'PO Token' in l]
            erros = [l for l in saida.splitlines() if l.startswith('ERROR')]
            erro = erros[0] if erros else ''
        except subprocess.TimeoutExpired:
            erro = 'O teste passou do tempo limite.'
        except Exception as exc:  # noqa: BLE001
            erro = f'Falha ao executar o teste: {exc}'

        etapas.append({
            'nome': 'Token gerado',
            'ok': bool(tokens),
            'detalhe': tokens[0][:140] if tokens else 'nenhum token foi pedido',
        })
        etapas.append({
            'nome': 'Download liberado',
            'ok': codigo == 0,
            'detalhe': 'funcionou' if codigo == 0 else (erro[:200] or 'bloqueado'),
        })

    if codigo == 0:
        veredito = 'ok'
        conselho = 'Tudo certo: o YouTube liberou o download.'
    elif not provedor['pronto']:
        veredito = 'provedor'
        conselho = 'Complete a instalação do provedor de PO Token.'
    elif not tem_sessao:
        veredito = 'cookies'
        conselho = 'Clique em "Conectar YouTube" e entre na sua conta.'
    elif tokens:
        veredito = 'ip-bloqueado'
        conselho = (
            'O token foi gerado e a sessão está válida, mas o YouTube ainda bloqueia: '
            'a restrição é do seu endereço de IP, não dos cookies. O caminho que '
            'funciona é trocar de rede — use o hotspot do celular para baixar, ou '
            'aguarde algumas horas até a marcação expirar.'
        )
    else:
        veredito = 'indefinido'
        conselho = 'Não foi possível concluir o diagnóstico. Veja o detalhe da etapa que falhou.'

    return {
        'veredito': veredito,
        'conselho': conselho,
        'etapas': etapas,
        'provedor': provedor,
        'tem_sessao': tem_sessao,
        'erro': erro[-400:] if erro else '',
    }
