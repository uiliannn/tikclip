"""Publicação direta no TikTok por automação local do navegador (gratuita, sem API paga).

Abre o Chrome (ou Edge) instalado no computador com um perfil próprio e persistente do TikClip
(`<data>/tiktok-browser-profile`), entra no TikTok Studio, envia o .mp4, preenche a legenda
(título + descrição + #hashtags) e clica em Publicar.

- Na primeira vez a janela fica aberta esperando você fazer login no TikTok (até 10 minutos).
  Nas próximas vezes a sessão já está salva no perfil e a publicação é automática.
- O Playwright fica em `<data>/tiktok-runtime` (fora da pasta do app, sobrevive a atualizações)
  e é instalado sozinho na primeira publicação se não existir.
- Uma publicação por vez: o mesmo perfil não pode ser aberto por duas janelas.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

UPLOAD_URL = 'https://www.tiktok.com/tiktokstudio/upload?from=webapp&lang=pt-BR'
LOGIN_TIMEOUT_S = 600
UPLOAD_TIMEOUT_S = 1200
CAPTION_LIMIT = 2200

_lock = threading.Lock()


class TikTokLocalError(RuntimeError):
    pass


def _data_dir() -> Path:
    if os.getenv('TIKCLIP_DATA_DIR'):
        return Path(os.getenv('TIKCLIP_DATA_DIR'))
    appdata_tikclip = Path(os.getenv('APPDATA', str(Path.home() / 'AppData' / 'Roaming'))) / 'TikClip'
    if (appdata_tikclip / 'tiktok-browser-profile').exists() or appdata_tikclip.is_dir():
        return appdata_tikclip
    try:
        from backend.core.path_utils import get_data_directory
        return Path(get_data_directory())
    except Exception:  # noqa: BLE001
        return appdata_tikclip


def runtime_dir() -> Path:
    return _data_dir() / 'tiktok-runtime'


ACCOUNTS_FILENAME = 'tiktok-accounts.json'


def accounts_file() -> Path:
    return _data_dir() / ACCOUNTS_FILENAME


def list_accounts() -> list[dict[str, Any]]:
    """Retorna a lista de contas do TikTok cadastradas. Garante a Conta Principal."""
    path = accounts_file()
    accounts = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, list):
                accounts = data
        except Exception as e:
            logger.warning('Falha ao ler tiktok-accounts.json: %s', e)

    has_default = any(a.get('id') == 'default' for a in accounts)
    if not has_default:
        accounts.insert(0, {'id': 'default', 'name': 'Conta Principal', 'created_at': time.time()})
        try:
            path.write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass

    return accounts


def add_account(name: str) -> dict[str, Any]:
    """Cadastra uma nova conta para login e publicação isolada no TikTok."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError('O nome da conta não pode ser vazio.')

    accounts = list_accounts()
    if any(a.get('name', '').lower() == clean_name.lower() for a in accounts):
        raise ValueError(f'Já existe uma conta com o nome "{clean_name}".')

    slug = re.sub(r'[^a-zA-Z0-9_-]', '_', clean_name).strip('_').lower()
    acc_id = f"conta_{slug}" if slug else f"conta_{uuid.uuid4().hex[:6]}"
    if any(a.get('id') == acc_id for a in accounts):
        acc_id = f"{acc_id}_{uuid.uuid4().hex[:4]}"

    p_dir = profile_dir(acc_id)
    p_dir.mkdir(parents=True, exist_ok=True)

    new_acc = {
        'id': acc_id,
        'name': clean_name,
        'created_at': time.time()
    }
    accounts.append(new_acc)
    accounts_file().write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding='utf-8')
    return new_acc


def remove_account(account_id: str) -> bool:
    """Remove uma conta da lista (a Conta Principal nunca pode ser removida)."""
    if account_id in ('default', '', None):
        raise ValueError('A Conta Principal não pode ser removida.')

    accounts = list_accounts()
    new_accounts = [a for a in accounts if a.get('id') != account_id]
    if len(new_accounts) == len(accounts):
        return False

    accounts_file().write_text(json.dumps(new_accounts, indent=2, ensure_ascii=False), encoding='utf-8')
    return True


def profile_dir(account: str | None = None) -> Path:
    """Retorna o diretório do perfil de navegador da conta especificada.
    
    'default' ou None usa a pasta padrão <data>/tiktok-browser-profile (onde a conta atual já está salva).
    Contas adicionais usam <data>/tiktok-browser-profiles/<account_id>.
    """
    if not account or account == 'default':
        path = _data_dir() / 'tiktok-browser-profile'
    else:
        path = _data_dir() / 'tiktok-browser-profiles' / account
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_runtime() -> None:
    """Coloca o Playwright no sys.path; instala em tiktok-runtime se ainda não existir."""
    target = str(runtime_dir())
    if target not in sys.path:
        sys.path.insert(0, target)
    try:
        import playwright.sync_api  # noqa: F401
        return
    except ImportError:
        pass
    logger.info('Instalando Playwright em %s (primeira publicação local no TikTok)', target)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    proc = subprocess.run([sys.executable, '-m', 'pip', 'install', '--target', target, 'playwright',
                           '--disable-pip-version-check', '--no-warn-script-location'],
                          capture_output=True, text=True, timeout=900, creationflags=flags)
    if proc.returncode:
        raise TikTokLocalError('Não foi possível instalar o Playwright: ' + (proc.stderr or proc.stdout)[-400:])
    import importlib
    importlib.invalidate_caches()
    import playwright.sync_api  # noqa: F401


def _channel() -> str:
    """Usa o navegador já instalado: nada de baixar Chromium.

    Edge primeiro: versões recentes do Chrome fecham na hora quando abertas por automação.
    """
    candidates = [
        ('msedge', [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe']),
        ('chrome', [r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
                    str(Path(os.getenv('LOCALAPPDATA', '')) / 'Google/Chrome/Application/chrome.exe')]),
    ]
    for channel, paths in candidates:
        if any(Path(p).is_file() for p in paths):
            return channel
    return 'msedge'


def status(account: str | None = None) -> dict[str, Any]:
    p_dir = profile_dir(account)
    return {'mode': 'local_browser', 'browser': _channel(), 'account': account or 'default',
            'profile': str(p_dir), 'logged_in_before': any(p_dir.iterdir()) if p_dir.exists() else False}


def login_session(timeout_s: int = LOGIN_TIMEOUT_S, account: str | None = None) -> bool:
    """Abre o TikTok Studio para fazer login uma vez; fecha sozinho quando a sessão estiver pronta."""
    _ensure_runtime()
    from playwright.sync_api import sync_playwright
    p_dir = profile_dir(account)
    with _lock, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(p_dir), channel=_channel(), headless=False, viewport=None, locale='pt-BR',
            args=['--start-maximized', '--disable-blink-features=AutomationControlled'],
            ignore_default_args=['--enable-automation'],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(UPLOAD_URL, wait_until='domcontentloaded')
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    page.locator('input[type="file"]').first.wait_for(state='attached', timeout=5000)
                    time.sleep(2)
                    return True
                except Exception:  # noqa: BLE001
                    if 'tiktokstudio/upload' not in page.url and 'login' not in page.url and 'tiktok.com' in page.url:
                        try:
                            page.goto(UPLOAD_URL, wait_until='domcontentloaded')
                        except Exception:  # noqa: BLE001
                            pass
            return False
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


def _click_first(page, selectors: list[str], timeout: float = 3000) -> bool:
    for selector in selectors:
        try:
            target = page.locator(selector).first
            if target.is_visible(timeout=timeout):
                target.click()
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _dismiss_overlays(page) -> None:
    try:
        page.evaluate("""() => {
            const portal = document.getElementById("react-joyride-portal");
            if (portal) portal.remove();
            document.querySelectorAll(".react-joyride__overlay, .react-joyride__tooltip, [data-test-id='overlay']").forEach(e => e.remove());
        }""")
    except Exception:
        pass
    _click_first(page, [
        'button[aria-label="Close"]',
        'button[aria-label="Fechar"]',
        'button:has-text("Entendi")',
        'button:has-text("Pular")',
        'button:has-text("Skip")',
        'button:has-text("Got it")',
        '.react-joyride__tooltip button',
    ], timeout=800)


def _upload_cover(page, cover_file: Path, progress: Callable[[str], None] | None = None) -> None:
    """Tenta selecionar a capa personalizada no TikTok Studio."""
    if not cover_file or not Path(cover_file).is_file():
        return
    if progress:
        progress('aguardando processamento do vídeo para anexar capa')
    _dismiss_overlays(page)

    # 1. Espera o upload terminar na caixa de capa (Enviando... / Uploading...)
    tip = page.locator('.cover-info-tip').first
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        _dismiss_overlays(page)
        try:
            if not tip.is_visible(timeout=1000):
                break
            txt = tip.inner_text().strip()
            if "Enviando" not in txt and "Upload" not in txt and "upload" not in txt:
                break
        except Exception:
            break
        time.sleep(1.5)

    if progress:
        progress('enviando capa personalizada')

    _dismiss_overlays(page)
    time.sleep(0.5)

    # 2. Procurar botão de editar capa e clicar com force=True
    cover_btn_selectors = [
        '.edit-container',
        '[data-e2e="cover_container"] .edit-container',
        'div.edit-container:has-text("Editar capa")',
        '[data-e2e="cover_container"]',
        'div:has-text("Editar capa")',
        'button:has-text("Editar capa")',
        'div:has-text("Edit cover")',
        'button:has-text("Edit cover")',
    ]
    opened = False
    for sel in cover_btn_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                _dismiss_overlays(page)
                loc.click(force=True)
                opened = True
                break
        except Exception:
            pass

    if not opened:
        logger.warning('Não encontrou botão de editar capa no TikTok Studio')
        return

    time.sleep(1.5)
    _dismiss_overlays(page)

    # 3. Esperar o modal de edição de capa
    dialog = page.locator('div[role="dialog"]').first
    try:
        dialog.wait_for(state='visible', timeout=10000)
    except Exception:
        dialog = page

    # 4. Enviar o arquivo de imagem diretamente para o input[type="file"] sem abrir janela nativa do Windows
    cover_inputs = dialog.locator('input[type="file"]')
    if cover_inputs.count() == 0:
        cover_inputs = page.locator('input[type="file"][accept*="image"]')

    if cover_inputs.count() > 0:
        cover_inputs.first.set_input_files(str(cover_file))
        logger.info('Capa enviada diretamente ao input[type=file]')
    else:
        # Fallback interceptando o seletor pelo DevTools para não abrir diálogo do Windows
        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                dialog.locator('.ImageUpload__root, *:has-text("Carregar capa")').last.click(force=True)
            fc_info.value.set_files(str(cover_file))
            logger.info('Capa enviada via file_chooser interceptado')
        except Exception as e:
            logger.warning('Falha ao interceptar seletor de arquivos: %s', e)

    time.sleep(2)
    # 5. Confirmar / Salvar no modal
    _click_first(dialog, [
        'button:has-text("Salvar")',
        'button:has-text("Confirmar")',
        'button:has-text("Concluir")',
        'button:has-text("Done")',
        'button:has-text("Save")',
    ], timeout=5000)
    time.sleep(1.5)
    logger.info('Capa personalizada aplicada com sucesso: %s', cover_file.name)


def _fill_caption(page, text: str, tags: list[str]) -> None:
    _dismiss_overlays(page)
    editor = page.locator('div[contenteditable="true"]').first
    editor.wait_for(state='visible', timeout=120_000)
    _dismiss_overlays(page)
    try:
        editor.click(timeout=5000)
    except Exception:
        _dismiss_overlays(page)
        editor.click(force=True)
    page.keyboard.press('Control+A')
    page.keyboard.press('Delete')
    time.sleep(0.3)
    if text:
        page.keyboard.insert_text(text)
    for tag in tags:
        # Digitar a hashtag faz o TikTok reconhecê-la; o espaço fecha a lista de sugestões.
        page.keyboard.insert_text(' ')
        page.keyboard.type(f'#{tag}', delay=60)
        time.sleep(1.2)
        page.keyboard.press('Space')
        time.sleep(0.3)


def _wait_post_enabled(page, deadline: float, progress: Callable[[str], None] | None) -> Any:
    button = page.locator('button[data-e2e="post_video_button"]').first
    last_note = 0.0
    while time.monotonic() < deadline:
        _dismiss_overlays(page)
        try:
            if button.is_visible(timeout=1000):
                disabled = button.get_attribute('aria-disabled') == 'true' or button.is_disabled()
                if not disabled:
                    return button
        except Exception:  # noqa: BLE001
            pass
        if progress and time.monotonic() - last_note > 15:
            progress('enviando vídeo ao TikTok')
            last_note = time.monotonic()
        time.sleep(2)
    raise TikTokLocalError('O envio do vídeo ao TikTok não terminou a tempo')


def publish_video(video_path: str | Path, title: str, description: str = '', tags: list[str] | None = None,
                  cover_path: str | Path | None = None,
                  *, account: str | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Publica um vídeo no TikTok pelo TikTok Studio. Retorna {'status': 'completed'|'submitted', 'url', 'message'}."""
    video = Path(video_path)
    if not video.is_file() or not video.stat().st_size:
        raise TikTokLocalError(f'Vídeo não encontrado: {video}')
    tags = [t.strip().lstrip('#').replace(' ', '') for t in (tags or []) if t and t.strip()]
    body = '\n\n'.join(part for part in (title.strip(), (description or '').strip()) if part)
    tag_text = ' '.join(f'#{t}' for t in tags)
    if len(body) + len(tag_text) + 2 > CAPTION_LIMIT:
        body = body[:max(0, CAPTION_LIMIT - len(tag_text) - 3)].rstrip()

    _ensure_runtime()
    from playwright.sync_api import sync_playwright

    with _lock:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(profile_dir(account)), channel=_channel(), headless=False, viewport=None, locale='pt-BR',
                args=['--start-maximized', '--disable-blink-features=AutomationControlled'],
                ignore_default_args=['--enable-automation'],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(60_000)
                if progress:
                    progress('abrindo TikTok Studio')
                page.goto(UPLOAD_URL, wait_until='domcontentloaded')

                # Espera o campo de upload; se não estiver logado, a janela fica aberta para o login.
                file_input = page.locator('input[type="file"]').first
                deadline = time.monotonic() + LOGIN_TIMEOUT_S
                while True:
                    try:
                        file_input.wait_for(state='attached', timeout=5000)
                        break
                    except Exception:  # noqa: BLE001
                        if time.monotonic() > deadline:
                            raise TikTokLocalError('Login no TikTok não foi concluído (faça login na janela aberta e publique de novo)')
                        if progress:
                            progress('aguardando login no TikTok (faça login na janela do navegador)')
                        if 'tiktokstudio/upload' not in page.url and 'login' not in page.url:
                            try:
                                page.goto(UPLOAD_URL, wait_until='domcontentloaded')
                            except Exception:  # noqa: BLE001
                                pass

                if progress:
                    progress('enviando arquivo')
                file_input.set_input_files(str(video))

                if progress:
                    progress('preenchendo legenda')
                _fill_caption(page, body, tags)

                # Se houver capa personalizada (.jpg), tenta enviar no TikTok Studio
                if cover_path and Path(cover_path).is_file():
                    try:
                        _upload_cover(page, Path(cover_path), progress)
                    except Exception as cover_err:
                        logger.warning('Não foi possível definir capa customizada no TikTok: %s', cover_err)

                _dismiss_overlays(page)
                button = _wait_post_enabled(page, time.monotonic() + UPLOAD_TIMEOUT_S, progress)
                time.sleep(1.5)
                if progress:
                    progress('publicando')
                _dismiss_overlays(page)
                try:
                    button.click(timeout=5000)
                except Exception:
                    _dismiss_overlays(page)
                    button.click(force=True)

                # Confirmar publicação em modal de verificação (se aparecer "Publicar agora")
                confirm_deadline = time.monotonic() + 15
                while time.monotonic() < confirm_deadline:
                    _dismiss_overlays(page)
                    clicked = _click_first(page, [
                        'button:has-text("Publicar agora")',
                        'button:has-text("Post now")',
                        'div[role="dialog"] button:has-text("Publicar agora")',
                        'div[role="dialog"] button:has-text("Publicar")',
                    ], timeout=1500)
                    if clicked:
                        time.sleep(1)
                        break
                    time.sleep(1)

                done = time.monotonic() + 120
                while time.monotonic() < done:
                    if '/tiktokstudio/content' in page.url or '/tiktokstudio/post' in page.url:
                        return {'status': 'completed', 'url': 'https://www.tiktok.com/tiktokstudio/content',
                                'message': 'Publicado no TikTok'}
                    try:
                        if page.get_by_text('Seu vídeo foi publicado').first.is_visible(timeout=500) or \
                           page.get_by_text('Your video has been uploaded').first.is_visible(timeout=500) or \
                           page.get_by_text('Vídeo publicado').first.is_visible(timeout=500):
                            return {'status': 'completed', 'url': 'https://www.tiktok.com/tiktokstudio/content',
                                    'message': 'Publicado no TikTok'}
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(2)
                return {'status': 'submitted', 'url': 'https://www.tiktok.com/tiktokstudio/content',
                        'message': 'Publicação enviada; confirme no TikTok Studio'}
            finally:
                try:
                    time.sleep(2)
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
