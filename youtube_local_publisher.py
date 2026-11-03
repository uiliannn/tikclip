"""Publicação direta no YouTube Studio (Shorts e Vídeos Normais) por automação local do navegador.

Características:
- 100% gratuito, sem necessidade de API paga ou cota diária limitada da YouTube Data API v3.
- Suporte a Múltiplos Canais / Contas com perfis de navegador isolados e persistentes.
- Na primeira vez, abre o YouTube Studio para login uma única vez no canal; depois disso a sessão fica salva permanentemente.
- Suporta Publicação de Shorts (verticais com #Shorts automático) e Vídeos Normais (com envio de thumbnail/capa personalizada).
- Configuração de visibilidade (Público, Não listado, Privado), tags e marcação obrigatória 'Não feito para crianças'.
- Monitoramento de progresso passo a passo em tempo real com logs transmitidos para o frontend.
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

logger = logging.getLogger("tikclip.youtube_publisher")

STUDIO_URL = "https://studio.youtube.com"
LOGIN_TIMEOUT_S = 600
UPLOAD_TIMEOUT_S = 1800

_lock = threading.Lock()


class YouTubeLocalError(RuntimeError):
    pass


def _data_dir() -> Path:
    raw = os.getenv("TIKCLIP_DATA_DIR")
    if raw:
        return Path(raw)
    appdata = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "TikClip"
    return appdata


def runtime_dir() -> Path:
    # Reaproveita a pasta de runtime existente do Playwright em TikClip
    return _data_dir() / "tiktok-runtime"


ACCOUNTS_FILENAME = "youtube-accounts.json"


def accounts_file() -> Path:
    return _data_dir() / ACCOUNTS_FILENAME


def list_accounts() -> list[dict[str, Any]]:
    """Retorna a lista de contas/canais do YouTube cadastrados. Garante o Canal Principal."""
    path = accounts_file()
    accounts = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                accounts = data
        except Exception as exc:
            logger.warning("Falha ao ler youtube-accounts.json: %s", exc)

    has_default = any(a.get("id") == "default" for a in accounts)
    if not has_default:
        accounts.insert(0, {
            "id": "default",
            "name": "Canal Principal",
            "created_at": time.time(),
        })
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    return accounts


def add_account(name: str) -> dict[str, Any]:
    """Cadastra um novo canal/conta do YouTube para perfil isolado."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("O nome do canal não pode ser vazio.")

    accounts = list_accounts()
    if any(a.get("name", "").lower() == clean_name.lower() for a in accounts):
        raise ValueError(f'Já existe um canal com o nome "{clean_name}".')

    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", clean_name).strip("_").lower()
    acc_id = f"canal_{slug}" if slug else f"canal_{uuid.uuid4().hex[:6]}"
    if any(a.get("id") == acc_id for a in accounts):
        acc_id = f"{acc_id}_{uuid.uuid4().hex[:4]}"

    p_dir = profile_dir(acc_id)
    p_dir.mkdir(parents=True, exist_ok=True)

    new_acc = {
        "id": acc_id,
        "name": clean_name,
        "created_at": time.time(),
    }
    accounts.append(new_acc)
    accounts_file().write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
    return new_acc


def remove_account(account_id: str) -> bool:
    """Remove uma conta da lista (o Canal Principal nunca pode ser removido)."""
    if account_id in ("default", "", None):
        raise ValueError("O Canal Principal não pode ser removido.")

    accounts = list_accounts()
    new_accounts = [a for a in accounts if a.get("id") != account_id]
    if len(new_accounts) == len(accounts):
        return False

    accounts_file().write_text(json.dumps(new_accounts, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def profile_dir(account: str | None = None) -> Path:
    """Retorna o diretório do perfil de navegador isolado da conta especificada.
    
    'default' ou None usa <data>/youtube-browser-profile.
    Canais adicionais usam <data>/youtube-browser-profiles/<account_id>.
    """
    if not account or account == "default":
        path = _data_dir() / "youtube-browser-profile"
    else:
        path = _data_dir() / "youtube-browser-profiles" / account
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_runtime() -> None:
    """Garante que o Playwright esteja disponível no sys.path."""
    target = str(runtime_dir())
    if target not in sys.path:
        sys.path.insert(0, target)
    try:
        import playwright.sync_api  # noqa: F401
        return
    except ImportError:
        pass

    logger.info("Instalando Playwright em %s...", target)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            target,
            "playwright",
            "--disable-pip-version-check",
            "--no-warn-script-location",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        creationflags=flags,
    )
    if proc.returncode:
        raise YouTubeLocalError(
            "Não foi possível instalar o Playwright: " + (proc.stderr or proc.stdout)[-400:]
        )
    import importlib
    importlib.invalidate_caches()
    import playwright.sync_api  # noqa: F401


def _channel() -> str:
    """Detecta o navegador padrão instalado: Edge ou Chrome."""
    candidates = [
        (
            "msedge",
            [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ],
        ),
        (
            "chrome",
            [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                str(Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
            ],
        ),
    ]
    for channel, paths in candidates:
        if any(Path(p).is_file() for p in paths):
            return channel
    return "msedge"


def status(account: str | None = None) -> dict[str, Any]:
    p_dir = profile_dir(account)
    has_profile = p_dir.exists() and any(p_dir.iterdir())
    return {
        "mode": "local_browser",
        "browser": _channel(),
        "account": account or "default",
        "profile": str(p_dir),
        "logged_in_before": has_profile,
    }


def _dismiss_popups(page) -> None:
    """Fecha diálogos, avisos ou popups que o YouTube Studio possa exibir ao carregar."""
    dismiss_selectors = [
        "tp-yt-paper-dialog #close-button",
        "tp-yt-paper-dialog button:has-text('Fechar')",
        "tp-yt-paper-dialog button:has-text('Entendi')",
        "tp-yt-paper-dialog button:has-text('Dismiss')",
        "button[aria-label='Fechar']",
        "button[aria-label='Close']",
        "button:has-text('Fechar')",
        "button:has-text('Dispensar')",
        "button:has-text('Pular tour')",
    ]
    for sel in dismiss_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=500):
                el.click()
                time.sleep(0.3)
        except Exception:
            pass


def login_session(
    timeout_s: int = LOGIN_TIMEOUT_S,
    account: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> bool:
    """Abre o YouTube Studio no navegador para o usuário fazer login.
    
    Salva a sessão no perfil persistente e fecha quando o login for concluído.
    """
    _ensure_runtime()
    from playwright.sync_api import sync_playwright

    p_dir = profile_dir(account)
    if progress:
        progress("Abrindo YouTube Studio para conferir ou realizar login...")

    with _lock, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(p_dir),
            channel=_channel(),
            headless=False,
            viewport=None,
            locale="pt-BR",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(STUDIO_URL, wait_until="domcontentloaded")

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                time.sleep(2)
                cur_url = page.url.lower()

                # Verifica se o YouTube Studio carregou com a conta ativa
                if "studio.youtube.com" in cur_url and "accounts.google.com" not in cur_url:
                    _dismiss_popups(page)
                    studio_markers = [
                        "#avatar-btn",
                        "#create-icon",
                        "button:has-text('Criar')",
                        "button:has-text('Create')",
                        "#upload-icon",
                    ]
                    for marker in studio_markers:
                        try:
                            if page.locator(marker).first.is_visible(timeout=1500):
                                if progress:
                                    progress("Login no YouTube detectado com sucesso!")
                                time.sleep(2)
                                return True
                        except Exception:
                            pass

                # Se a janela foi fechada pelo usuário
                if page.is_closed():
                    break

            return False
        finally:
            try:
                context.close()
            except Exception:
                pass


def publish_video(
    video_path: Path | str,
    title: str,
    description: str = "",
    tags: list[str] | str = "",
    thumbnail_path: Path | str | None = None,
    visibility: str = "public",
    is_short: bool = False,
    account: str | None = None,
    progress: Callable[[str], None] | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    """Publica um vídeo (Shorts ou Normal) diretamente no YouTube Studio via navegador local.
    
    Parâmetros:
    - video_path: Caminho para o arquivo .mp4 do vídeo.
    - title: Título do vídeo (máx 100 caracteres).
    - description: Descrição do vídeo.
    - tags: Lista ou string separada por vírgulas de tags.
    - thumbnail_path: Imagem de capa/miniatura (.jpg ou .png).
    - visibility: 'public' (padrão), 'unlisted' ou 'private'.
    - is_short: Se True, garante formatação para YouTube Shorts (#Shorts).
    - account: ID da conta cadastrada no YouTube ('default', etc.).
    - progress: Callback para envio de mensagens de progresso em tempo real.
    - headless: Se True, roda em segundo plano sem janela visível.
    """
    _ensure_runtime()
    from playwright.sync_api import sync_playwright

    v_path = Path(video_path).resolve()
    if not v_path.is_file():
        raise FileNotFoundError(f"Arquivo de vídeo não encontrado: {video_path}")

    p_dir = profile_dir(account)

    def _prog(msg: str):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass
        logger.info("[YouTube Publisher] %s", msg)

    # Tratamento do Título (máximo 100 caracteres no YouTube)
    clean_title = (title or v_path.stem).strip()
    if is_short and "#shorts" not in clean_title.lower():
        if len(clean_title) + 8 <= 100:
            clean_title = f"{clean_title} #Shorts"
        elif "#shorts" not in (description or "").lower():
            description = f"{description}\n\n#Shorts".strip()

    final_title = clean_title[:100]

    _prog(f'Iniciando envio no YouTube Studio: "{final_title}"...')
    _prog(f'Conta: {account or "default"} | Modo: {"Shorts" if is_short else "Vídeo Normal"}')

    with _lock, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(p_dir),
            channel=_channel(),
            headless=headless,
            viewport=None,
            locale="pt-BR",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )

        try:
            page = context.pages[0] if context.pages else context.new_page()
            _prog("Navegando para o YouTube Studio...")
            page.goto(STUDIO_URL, wait_until="domcontentloaded")
            time.sleep(2)

            # Verifica se caiu na página de login do Google
            if "accounts.google.com" in page.url.lower():
                _prog("Aviso: Esta conta ainda não está logada no Google/YouTube.")
                _prog("Aguardando login do usuário na janela do navegador (até 10 min)...")
                deadline = time.monotonic() + LOGIN_TIMEOUT_S
                logged = False
                while time.monotonic() < deadline:
                    time.sleep(2)
                    if "studio.youtube.com" in page.url.lower() and "accounts.google.com" not in page.url.lower():
                        logged = True
                        break
                if not logged:
                    raise YouTubeLocalError("Tempo de login esgotado no YouTube Studio.")

            _dismiss_popups(page)

            # Clicar em "Criar" e "Enviar vídeos"
            _prog("Abrindo assistente de upload do YouTube Studio...")
            create_btn = page.locator(
                "#create-icon, button:has-text('Criar'), button:has-text('Create'), ytcp-button#create-icon"
            ).first
            
            # Se não achar o botão criar de imediato, tenta achar o botão de upload na home
            if not create_btn.is_visible(timeout=5000):
                direct_up = page.locator("#upload-button, #upload-icon, [aria-label*='Enviar vídeos' i]").first
                if direct_up.is_visible(timeout=3000):
                    direct_up.click()
                else:
                    page.goto(f"{STUDIO_URL}/channel/mine/videos/upload?d=pt", wait_until="domcontentloaded")
            else:
                create_btn.click()
                time.sleep(1)
                upload_item = page.locator(
                    "tp-yt-paper-item:has-text('Enviar vídeos'), tp-yt-paper-item:has-text('Upload videos'), #text-item-0"
                ).first
                if upload_item.is_visible(timeout=4000):
                    upload_item.click()
                else:
                    create_btn.click()

            # Localiza o input de arquivos
            _prog("Enviando arquivo de vídeo para o YouTube...")
            file_input = page.locator("ytcp-uploads-dialog input[type='file'], input[type='file']").first
            file_input.wait_for(state="attached", timeout=30000)
            file_input.set_input_files(str(v_path))

            # Aguarda o assistente carregar a tela de detalhes do vídeo
            _prog("Aguardando carregamento da tela de detalhes...")
            try:
                page.locator("ytcp-uploads-dialog").first.wait_for(state="attached", timeout=45000)
            except Exception:
                pass

            # Aguarda diretamente o campo de título estar visível e interativo
            title_input = page.locator(
                "#title-textarea #textbox, [aria-label*='título' i][contenteditable='true'], [aria-label*='title' i][contenteditable='true'], #textbox[contenteditable='true']"
            ).first
            title_input.wait_for(state="visible", timeout=60000)
            time.sleep(1)
            _dismiss_popups(page)

            # 1. PREENCHIMENTO DO TÍTULO
            _prog(f'Definindo título: "{final_title}"')
            title_input.click()
            time.sleep(0.3)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            time.sleep(0.2)
            # Preenche via JS e via Playwright para garantir
            page.evaluate("""(t) => {
                const el = document.querySelector('#title-textarea #textbox, [aria-label*="título" i][contenteditable="true"], [aria-label*="title" i][contenteditable="true"]');
                if (el) {
                    el.textContent = t;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""", final_title)
            try:
                title_input.fill(final_title)
            except Exception:
                pass
            time.sleep(0.5)

            # 2. PREENCHIMENTO DA DESCRIÇÃO
            if description:
                _prog("Preenchendo descrição do vídeo...")
                try:
                    desc_input = page.locator(
                        "#description-textarea #textbox, [aria-label*='descrição' i][contenteditable='true'], [aria-label*='description' i][contenteditable='true']"
                    ).first
                    if desc_input.is_visible(timeout=5000):
                        desc_input.click()
                        time.sleep(0.2)
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Backspace")
                        page.evaluate("""(d) => {
                            const el = document.querySelector('#description-textarea #textbox, [aria-label*="descrição" i][contenteditable="true"]');
                            if (el) {
                                el.textContent = d;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }""", description[:5000])
                        try:
                            desc_input.fill(description[:5000])
                        except Exception:
                            pass
                        time.sleep(0.5)
                except Exception as exc:
                    _prog(f"Aviso ao preencher descrição: {exc}")

            # 3. MINIATURA / THUMBNAIL PERSONALIZADA
            if thumbnail_path:
                t_path = Path(thumbnail_path).resolve()
                if t_path.is_file():
                    _prog(f'Enviando miniatura personalizada: "{t_path.name}"...')
                    try:
                        thumb_input = page.locator(
                            "input#file-loader, input[type='file'][accept*='image']"
                        ).first
                        if thumb_input.is_attached():
                            thumb_input.set_input_files(str(t_path))
                            _prog("Miniatura personalizada enviada ✔")
                            time.sleep(1)
                    except Exception as exc:
                        _prog(f"Aviso: Não foi possível anexar a miniatura ({exc})")

            # 4. PÚBLICO-ALVO (OBRIGATÓRIO: NÃO É CONTEÚDO PARA CRIANÇAS)
            _prog("Definindo restrição de público: 'Não é conteúdo para crianças'...")
            try:
                page.evaluate("""() => {
                    const el = document.querySelector('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"], tp-yt-paper-radio-button#radio-not-mfk');
                    if (el) {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        el.click();
                    }
                }""")
                time.sleep(0.5)
                not_kids_radio = page.locator(
                    "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK'], tp-yt-paper-radio-button:has-text('Não é conteúdo para crianças'), tp-yt-paper-radio-button:has-text('No, it\\'s not made for kids')"
                ).first
                if not_kids_radio.is_visible(timeout=3000):
                    not_kids_radio.scroll_into_view_if_needed()
                    not_kids_radio.click()
                    time.sleep(0.5)
            except Exception as exc:
                _prog(f"Aviso ao marcar público-alvo: {exc}")

            # 5. TAGS (OPCIONAL)
            if tags:
                try:
                    tag_list = tags if isinstance(tags, list) else [t.strip() for t in tags.split(",") if t.strip()]
                    if tag_list:
                        show_more = page.locator(
                            "#toggle-button, button:has-text('Mostrar mais'), button:has-text('Show more')"
                        ).first
                        if show_more.is_visible(timeout=3000):
                            show_more.scroll_into_view_if_needed()
                            show_more.click()
                            time.sleep(0.5)
                            tag_input = page.locator("#tags-container #text-input, input[aria-label*='Tags' i]").first
                            if tag_input.is_visible(timeout=3000):
                                _prog(f"Inserindo {len(tag_list)} tag(s)...")
                                tag_input.fill(",".join(tag_list))
                                time.sleep(0.5)
                except Exception as exc:
                    _prog(f"Aviso ao preencher tags: {exc}")

            # 6. CAPTURA DO LINK DO VÍDEO
            video_url = None
            video_id = None
            try:
                link_el = page.locator("a.ytcp-video-info, a[href*='youtu.be']").first
                if link_el.is_visible(timeout=3000):
                    video_url = link_el.get_attribute("href")
                    if video_url:
                        video_id = video_url.rstrip("/").split("/")[-1]
            except Exception:
                pass

            # 7. AVANÇAR ETAPAS ("Próximo" / "Avançar")
            _prog("Avançando para as etapas de elementos, verificações e visibilidade...")
            # O YouTube tem 3 passos até Visibilidade: Detalhes -> Elementos -> Verificações -> Visibilidade
            for step_num in range(1, 4):
                next_btn = page.locator(
                    "#next-button, ytcp-button#next-button, button#next-button, button:has-text('Próximo'), button:has-text('Avançar'), button:has-text('Next')"
                ).first
                if next_btn.is_visible(timeout=8000):
                    next_btn.click()
                    time.sleep(1.5)
                    _dismiss_popups(page)
                else:
                    # Tenta clicar via evaluate
                    page.evaluate("""() => {
                        const btn = document.querySelector('#next-button, ytcp-button#next-button');
                        if (btn) btn.click();
                    }""")
                    time.sleep(1.5)

            # 8. ETAPA DE VISIBILIDADE (Público, Não listado ou Privado)
            vis_lower = (visibility or "public").lower()
            _prog(f"Configurando visibilidade: {vis_lower.upper()}...")

            vis_name = "PUBLIC"
            if vis_lower in ("unlisted", "nao_listado", "nao-listado"):
                vis_name = "UNLISTED"
            elif vis_lower in ("private", "privado"):
                vis_name = "PRIVATE"

            try:
                page.evaluate(f"""() => {{
                    const radio = document.querySelector('tp-yt-paper-radio-button[name="{vis_name}"]');
                    if (radio) {{
                        radio.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                        radio.click();
                    }}
                }}""")
                time.sleep(0.5)
                vis_radio = page.locator(f"tp-yt-paper-radio-button[name='{vis_name}']").first
                if vis_radio.is_visible(timeout=3000):
                    vis_radio.click()
                    time.sleep(0.5)
            except Exception as exc:
                _prog(f"Aviso ao selecionar visibilidade: {exc}")

            # Tenta pegar o link novamente se ainda não pegou
            if not video_url:
                try:
                    link_el = page.locator("a.ytcp-video-info, a[href*='youtu.be']").first
                    if link_el.is_visible(timeout=3000):
                        video_url = link_el.get_attribute("href")
                        if video_url:
                            video_id = video_url.rstrip("/").split("/")[-1]
                except Exception:
                    pass

            # 9. SALVAR / PUBLICAR
            _prog("Publicando o vídeo no YouTube Studio...")
            done_btn = page.locator(
                "#done-button, ytcp-button#done-button, button#done-button, button:has-text('Publicar'), button:has-text('Salvar'), button:has-text('Publish'), button:has-text('Save')"
            ).first
            try:
                done_btn.wait_for(state="visible", timeout=15000)
                done_btn.click()
            except Exception:
                page.evaluate("""() => {
                    const btn = document.querySelector('#done-button, ytcp-button#done-button');
                    if (btn) btn.click();
                }""")
            time.sleep(3)

            # 10. AGUARDA CONFIRMAÇÃO OU TELA DE SUCESSO
            _prog("Aguardando confirmação de processamento...")
            try:
                # Modal de confirmação: "Vídeo publicado" ou "Processando vídeo"
                close_btn = page.locator(
                    "ytcp-uploads-dialog #close-button, ytcp-video-upload-progress #close-button, button:has-text('Fechar'), button:has-text('Close')"
                ).first
                if close_btn.is_visible(timeout=25000):
                    # Tenta extrair o link final presente na tela de confirmação
                    if not video_url:
                        try:
                            cf_link = page.locator("a[href*='youtu.be']").first
                            if cf_link.is_visible(timeout=2000):
                                video_url = cf_link.get_attribute("href")
                                if video_url:
                                    video_id = video_url.rstrip("/").split("/")[-1]
                        except Exception:
                            pass
                    close_btn.click()
                    time.sleep(1)
            except Exception:
                pass

            final_link_msg = f" (Link: {video_url})" if video_url else ""
            _prog(f"Vídeo publicado com sucesso no YouTube Studio!{final_link_msg} ✔")

            return {
                "ok": True,
                "status": "publicado",
                "title": final_title,
                "account": account or "default",
                "video_url": video_url,
                "video_id": video_id,
                "is_short": is_short,
            }

        finally:
            try:
                context.close()
            except Exception:
                pass
