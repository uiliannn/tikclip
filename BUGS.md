# Auditoria de bugs — TikClip

Duas passadas de correção sobre todos os **267 arquivos `.py`** do repositório,
com `compileall`, `ruff` (fatal + F completa + AST de rotas) e leitura dirigida
das áreas críticas: dublagem, legendas, rotas, WebSocket e orquestração.

Status: **CORRIGIDO** · **ABERTO** (precisa de decisão/deploy).

---

## 1. Bugs corrigidos

### 1.1 [ALTA] Método duplicado na mesma classe quebrava o sync de canais

`backend/services/websocket_gateway_service.py`

`sync_user_subscriptions` existia **duas vezes dentro de `WebSocketGatewayService`**:
linha 97 `(user_id, channels: Set[str])` e linha 344 `(user_id, desired_task_ids: list[str])`.
A última definição vence, então a versão de canais era inexistente — e
`backend/api/v1/websocket.py:100`, que passa nomes de canal, executava a lógica de
task IDs. Assinatura de canais nunca funcionou.

**Correção:** renomeada para `sync_task_subscriptions` + chamador em `websocket.py:205`
ajustado. As duas coexistem.

### 1.2 [ALTA] `is_completed` / `is_running` sempre verdadeiros

`backend/models/task.py`

Definidos como `@property` (linhas 132–140) e depois como **métodos** (187–193). Os
métodos sobrescrevem as properties: `task.is_completed` virava *bound method*, sempre
truthy. Qualquer `if task.is_completed:` passaria mesmo em tarefa pendente.

**Correção:** duplicatas removidas; properties restauradas; `is_finished` adicionada
para o estado terminal (COMPLETED/FAILED/CANCELLED).

### 1.3 [ALTA] Rota duplicada fazia toda chamada com JSON retornar 422

`backend/api/v1/settings.py`

Dois handlers de `POST /paths/data-directory`: linha 360 (query params) e linha 592
(corpo JSON, com `response_model`). O FastAPI resolve na ordem de registro, então o
primeiro vencia e quem mandasse JSON recebia `422 Field required`.

**Correção:** handler antigo removido; sobrou o que usa `UpdateDataDirRequest`.
Há teste de regressão (item 3).

### 1.4 [ALTA] Pipeline gravava resultado **falso** com `status: completed`

`backend/services/processing_orchestrator.py` (linhas 31–172 antes da correção)

Quando `backend.pipeline` falhava ao importar, seis funções placeholder
(`run_step1_outline` … `run_step6_video`) escreviam JSON simulado no `output_path`:

```python
mock_output = {"outlines": [{"topic": "测试话题1", ...}], "status": "completed"}
```

O retorno dizia `"status": "skipped"`, mas o **arquivo gravado dizia `completed`**.
A etapa seguinte lia o arquivo e seguia como se tivesse dado certo — produzindo vídeo
vazio ou sem sentido em vez de falhar.

**Correção:** os placeholders agora levantam `RuntimeError` nomeando a etapa e a causa
do import. O aviso no log permanece. 107 linhas de mock removidas.

### 1.5 [ALTA] Caminho absoluto da máquina do autor no código

`video_translator.py:37`

```python
Path(r"C:\Users\Uilian\.gemini\antigravity\scratch\tiktok-downloader-web\downloads")
```

Quebra em qualquer outro computador — e o projeto é público.

**Correção:** `TIKCLIP_DOWNLOADS_DIR` → pasta legada (se existir) → `%APPDATA%/TikClip/tiktok-downloads`.
Comportamento preservado na máquina de quem já usa.

### 1.6 [MÉDIA] `requirements.txt` incompleto quebrava a instalação

Declarava 18 pacotes; o código importa 40 módulos externos. Faltavam **`edge-tts`** e
**`faster-whisper`** — as duas dependências da dublagem. Também `psutil`, `pysrt`, `yaml`,
`mcp`, `sentry-sdk`, `qrcode`, `aiohttp`, `anyio`, `starlette`, `pytz`.

**Correção:** reorganizado por área; pesados (`torch`, `funasr`, `opencv`, `celery`,
`redis`, `dashscope`) em bloco opcional comentado.

### 1.7 [MÉDIA] 82 `except` silenciosos (50 já tinham logger, 32 não)

Nenhum deles registrava nada: o erro acontecia e o fluxo seguia como se estivesse tudo bem.
7 eram `except:` nus, que ainda capturavam `KeyboardInterrupt` e `SystemExit` — Ctrl+C
durante download virava "erro recuperável".

**Correção:** os 7 nus viraram `except Exception`; os 82 silenciosos passaram a registrar
`logger.debug(..., exc_info=True)`. Em 8 arquivos sem logger (`server.py`, `api_ext.py`,
`tiktok_library.py`, `youtube_login.py`, `desktop_main.py`, `database.py`,
`studio/readiness.py`, `api/v1/studio.py`) foi criado um logger de módulo no cabeçalho.
**Zero `except` silenciosos restantes.**

### 1.8 [MÉDIA] `datetime.utcnow()` em 81 pontos (deprecado no 3.12+)

`utcnow()` retorna datetime *naive*. Trocar direto por `datetime.now(timezone.utc)`
devolveria *aware* e comparações com datas já gravadas no SQLite passariam a levantar
`TypeError` — quebrando a listagem de tarefas.

**Correção:** helper [`backend/utils/time_utils.py`](backend/utils/time_utils.py) com
`utcnow()` (naive, em UTC — mesmo tipo de antes, sem warning) e `utcnow_aware()` para
código novo. 81 chamadas migradas em 24 arquivos. Quando o banco migrar para timestamps
com timezone, o ponto de mudança é único.

### 1.9 [MÉDIA] `backend.services` exigia SQLAlchemy para importar qualquer submódulo

`backend/services/__init__.py` importava `.base` de forma ansiosa → `import
backend.services.subtitle_timing` (módulo puro) falhava com `ModuleNotFoundError:
sqlalchemy` em ambiente limpo, e nada de `backend.services` podia ser testado isolado.

**Correção:** imports sob demanda com `__getattr__` (PEP 562). `from backend.services
import BaseService` continua funcionando.

### 1.10 [MÉDIA] `backend/pytest.ini` apontava para pasta inexistente

`testpaths = tests` relativo a `backend/`, onde não existe `tests/`.

**Correção:** arquivo removido; marcadores (`slow`, `integration`, `unit`) migrados para
`[tool.pytest.ini_options]` no `pyproject.toml`, que agora é a única fonte.

### 1.11 [BAIXA] 365 imports/variáveis não usados

`ruff --fix` em F401/F841/F541: **365 correções em 142 arquivos** (319 imports, 23 f-strings
sem placeholder, 23 variáveis). Verificado depois: `F821` limpo (nenhum nome ficou órfão) e
`compileall` exit 0. Nenhum import de efeito colateral (sentry/setup/register) foi removido.

### 1.12 [BAIXA] `ext_clip_cover` definido duas vezes

`api_ext.py:305` (GET) e `:1350` (POST) — **não era bug** (rotas distintas), mas o nome
repetido confundia e o ruff acusava `F811`. O POST foi renomeado para `ext_clip_cover_upload`.

### 1.13 [BAIXA] Sem lockfile

**Correção:** [`requirements.lock`](requirements.lock) com **51 pacotes fixados**, gerado
por `pip install --dry-run --report`. O cabeçalho declara o escopo: resolução feita em
CPython 3.13 / win32 / AMD64. Para lock reproduzível em qualquer plataforma, migrar para
`uv` + `uv.lock` (o pip resolve por plataforma).

---

## 2. O que permanece aberto

### 2.1 [ARQUITETURAL] Três camadas de rotas montadas ao mesmo tempo

`server.py` registra três árvores:

| prefixo | módulo | tamanho |
| --- | --- | --- |
| `/api/v1` | `backend/api/v1/*` | ~30 arquivos |
| `/api/v1/ext` | `api_ext.py` | 87 KB |
| `/api/v1/supo` | `supo_api.py` | 60 KB |

Não é bug — todas carregam. Mas a mesma feature tende a existir em dois lugares e só uma
costuma ser atualizada. Consolidação exige decidir qual árvore é canônica e migrar o
frontend; é refactor, não correção pontual. **Sugestão:** congelar `api_ext`/`supo_api`
(só bugfix), não adicionar rota nova ali, e migrar por feature.

### 2.2 [BAIXA] 15 variáveis atribuídas e não usadas

O `--fix` do ruff não as removeu porque são correções marcadas como *unsafe* (mudam
semântica em alguns casos). Exemplos que valem revisão manual:
`processing_orchestrator.py` (step 1 recebe `srt_path` e não usa),
`studio/jobs.py` (`coverage` calculado e descartado),
`bilibili_service.py` (`file_size` calculado e descartado).

### 2.3 [BAIXA] 2 redefinições benignas

`youtube.py` e `account_health_service.py` reimportam `asyncio` localmente. Sem efeito
prático; ficaram fora do baseline do lint de propósito.

### 2.4 [BAIXA] Estilo (E712/E741/E731, ~23 ocorrências)

`== True`, variáveis chamadas `l`/`I`, lambda atribuído a nome. Só estilo — não entram no
gate, aparecem no relatório do CI.

---

## 3. O que foi adicionado

| arquivo | função |
| --- | --- |
| `backend/services/subtitle_timing.py` | SRT a partir dos `WordBoundary` do edge-tts + fallbacks |
| `backend/utils/time_utils.py` | `utcnow()` compatível e `utcnow_aware()` |
| `tests/test_subtitle_timing.py` | 62 testes do módulo puro |
| `tests/test_video_translator_srt.py` | 11 testes de integração do SRT |
| `tests/test_no_duplicate_routes.py` | 2 testes que detectam rota duplicada via AST (regressão do item 1.3) |
| `tests/conftest.py` | raiz do repo no `sys.path` |
| `.github/workflows/ci.yml` | compile + lint fatal + testes, Ubuntu/Windows × Python 3.11/3.13 |
| `pyproject.toml` | config de ruff (baseline) e pytest |
| `requirements-dev.txt` | ruff, pytest, pytest-cov |
| `requirements.lock` | 51 pacotes fixados |

### A mudança no pipeline de dublagem

Antes: `edge_tts...save()`, que **descarta** os `WordBoundary` — os únicos com o tempo
real de cada palavra. A legenda usava os tempos do Whisper sobre o vídeo original.

Agora: `stream()` coleta os `WordBoundary`, grava `<fala>.words.json` ao lado do áudio e o
SRT nasce deles, ancorado no início do trecho e cortado no início do próximo.

Degradação em três níveis: com sidecar → por palavra; sem sidecar → tempos do Whisper;
sem o módulo → escritor inline. A dublagem nunca falha por causa da legenda.

---

## 4. Como verificar

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium

python -m pytest tests --cov=backend.services.subtitle_timing --cov-fail-under=90
python -m ruff check .          # baseline: só erros fatais
python -m compileall -q . -x "(__pycache__|\.git)"
```

Resultado na máquina do autor (Windows, Python 3.13.16):

```
75 passed
backend/services/subtitle_timing.py   208 stmts   11 miss   95%
Required test coverage of 90% reached. Total coverage: 94.71%
ruff (E9,F63,F7,F82,F821): All checks passed!
compileall: exit 0
except silenciosos restantes: 0
```

> **Antes de commitar:** 168 arquivos foram tocados pelo autofix de imports. Vale rodar o
> app uma vez (`python server.py`) e abrir a UI antes de publicar, para confirmar que
> nenhum import removido fazia falta em runtime.

---

## 5. Achados ao subir o app de verdade

Rodar `python server.py` expôs problemas que lint e teste não pegam.

### 5.1 [ALTA] O `backend/` do repositório não era o que rodava

`server.py` inseria `HERE`, `STUDIO_APP`, `STUDIO_RUNTIME` e `STUDIO_ROOT` com
`insert(0)` em sequência — o último inserido fica na frente. Resultado: `import backend`
resolvia para `C:\Users\Uilian\Desktop\tikclip-studio\backend`, e **todo o código deste
repositório (inclusive as correções)** era ignorado. Confirmado na prática: o app
quebrava em `tikclip-studio\backend\api\v1\tasks.py`.

**Correção:** `HERE` primeiro; as pastas do Studio entram como `append` e só se existirem.
`STUDIO_ROOT` também deixou de ser caminho absoluto (env `TIKCLIP_STUDIO_ROOT` > pasta
irmã do repositório).

### 5.2 [ALTA] Auto-publicação nunca disparava — três bugs encadeados

`channel_monitor._verificar_auto_publicacoes`

1. Filtrava `job["type"] in ("render", "export")`, mas os jobs gravados no `studio.json`
   **não têm o campo `type`** (vem `None`). A lista saía vazia, `all_done` era sempre
   falso e o projeto nunca era publicado.
2. Mesmo passando, chamava `api_ext._iniciar_publicacao_lote` — função **inexistente**
   (`AttributeError` engolido pelo `except`, projeto marcado como `failed`).
3. O lote era "tem vídeo", então **reincluía cortes já publicados** e repostaria o mesmo
   vídeo.

**Correções:** predicado `_projeto_pronto_para_publicar()` baseado em
`generation.status` (com fallback no estado das variantes); chamada real a
`api_ext.ext_publish_batch(...)`; e filtro por `status == 'pronto'`, que exclui
`publicado`.

Medido em modo seco, sem publicar: **8 projetos / 59 cortes** prontos para a conta
`meuscortes`.

**Trava de segurança:** `TIKCLIP_DISABLE_AUTO_PUBLISH=1` desliga só a publicação
automática. O servidor está rodando com ela **ligada** — remover e reiniciar publica o
backlog.

### 5.3 [MÉDIA] Guard de logging existia e nunca era chamado

`_proteger_logging_utf8()` estava definido em `server.py` e nunca invocado. Cada log com
acento ou caractere chinês no console cp1252 gerava um traceback `UnicodeEncodeError` de
~60 KB (o log de startup tinha 24 KB de ruído). **Correção:** chamada no início de
`main()`.

### 5.4 [MÉDIA] Monitor de canais falhava em 100% dos canais

`_get_ytdlp_cmd()` devolvia a string `"yt-dlp"` quando o executável não estava no PATH;
`subprocess.run` estourava `[WinError 2]` nos 11 canais, a cada ciclo. **Correção:**
retorna lista e cai para `python -m yt_dlp`, que funciona com o pacote instalado.

### 5.5 [BAIXA] `celery`/`redis` eram dependência de boot, não opcionais

`backend/api/v1/tasks.py` → `task_queue_service` importa `celery.result` no topo. Sem
celery o servidor **não sobe**. Movidos para o bloco núcleo do `requirements.txt`.

### 5.6 [BAIXA] Operation ID duplicado no OpenAPI

`@router.api_route(..., methods=['GET','POST'])` gera o mesmo `operation_id` para os dois
métodos — aviso do FastAPI e problema para geradores de client. **Correção:** duas rotas
com `operation_id` explícito.

### 5.7 [OBSERVAÇÃO] Payloads pesados na listagem

`GET /api/v1/projects/` devolve **209 KB** para 8 projetos e `/api/v1/tasks/` **433 KB**,
porque as miniaturas vão embutidas em base64 no JSON. Funciona, mas a lista fica lenta com
o tempo. O caminho saudável é servir a capa por URL (já existe `/cover`) em vez de inline.

---

## 6. Funcionalidade nova: limpar publicados na Biblioteca

- **Backend:** `DELETE /api/v1/ext/projects/{pid}/clips/{vid}` — remove **apenas** o corte
  informado: registro em `studio.json`, arquivo de vídeo e capa. Guardas: 409 se ainda
  estiver renderizando e nunca apaga arquivo fora da pasta do projeto. O histórico de
  publicação é preservado.
- **UI:** botão **"🧹 Limpar publicados (N)"** no cabeçalho da Biblioteca, ao lado de
  "Nova geração". Aparece só quando há cortes publicados, lista o total no rótulo, pede
  confirmação explícita (avisando que os posts no TikTok não são afetados) e apaga apenas
  cortes com status `publicado` — os pendentes ficam.
- **Cachebuster** do `supoclip.js` atualizado para `20261110_lib2`.

Verificado com o app rodando: rota presente no OpenAPI, 404 correto para corte
inexistente sem apagar nada, e botão visível/renderizado na Biblioteca.

### 6.2 Disparo automático configurável nas Configurações

Antes, ligar/desligar a publicação automática só era possível por variável de ambiente.
Agora existe controle na UI:

- **Backend:** `GET`/`PUT /api/v1/ext/auto-publish`, persistido em
  `%APPDATA%/TikClip/auto_publish.json` como `{"enabled": bool, "interval_min": int}`
  (intervalo limitado a 1–1440 min). `channel_monitor` lê essa config a cada ciclo e
  usa o intervalo global; sem `enabled: true`, nada é enviado.
- **UI (Configurações → card "Disparo Automático"):** botão **Ativar / Travar**,
  campo de **intervalo entre disparos (min)**, botão de salvar e uma linha de status
  (`🟢 Ligado — envia a cada N min` / `⚪ Travado`). Se o ambiente tiver
  `TIKCLIP_DISABLE_AUTO_PUBLISH`, o card mostra que está bloqueado e desabilita os
  controles — a trava de ambiente continua tendo precedência.
- **Cachebuster** do `supoclip.js` em `20261110_auto1`.

Verificado com o app rodando: PUT persiste, GET reflete, `publish/state` mostra o
evento "ATIVADO/TRAVADO" no log do publicador, e o monitor não iniciou lote algum
(`total: 0`) enquanto a chave esteve ligada por poucos segundos no teste.

---

## 7. Modo História: vídeo vertical gerado do zero

Caminho **adicional** de produção, sem cortar, dublar ou traduzir nada do material
original. O motor de corte (`studio/render.py`), a dublagem e a publicação que já
funcionavam não foram alterados.

Pipeline: roteiro (LLM) -> 6 imagens (provedor já configurado para capas) -> 3 vozes
sintéticas (edge-tts) -> `timeline.json` -> HTML/CSS com `seek(t)` -> Playwright
quadro a quadro -> FFmpeg (H.264 + AAC, 1080x1920, 30 fps) -> publicação pelos
publicadores locais.

- **`backend/services/fiction/`**: `timeline.py` (schema do arquivo de composição),
  `script.py` (roteiro 80-100 palavras, recusa "caso real" e clone de voz),
  `voices.py` (3 vozes + `WordBoundary` + trilha única), `images.py` (6 cenas
  distintas), `builder.py` (roteiro -> timeline), `player.py` (HTML sem CDN),
  `render.py` (captura + verificação do MP4), `projects.py` (registro no projeto),
  `publish.py` (TikTok e YouTube pelos publicadores locais).
- **Rotas** (aditivas, em `api_ext.py`): `/ext/fiction/build`, `/state/{pid}`,
  `/projects`, `/player/{pid}`, `/video/{pid}`, `/publish`, `/publish/state`,
  `/publish/stop`. `/ext/publish/state` ganhou o bloco `fiction` ao lado do lote
  do TikTok.
- **UI**: item de menu "História" e `web/js/historia.js` (cachebuster
  `20261111_hist1`).
- **Regras garantidas por teste**: 6 cenas, 30-45 s, cenas contíguas, legenda
  dentro da cena e da fala, imagem repetida recusada, build que falha não fica
  "pronto", plataforma já publicada nunca é reenviada.
- **Trava por ambiente**: `TIKCLIP_DISABLE_AUTO_PUBLISH` bloqueia
  `/ext/fiction/publish` (o lote manual do TikTok segue com o comportamento
  anterior, de propósito).

### 7.1 Verificação de ponta a ponta (máquina do usuário)

Render real com TTS real: 39,28 s, 17 legendas vindas de `WordBoundary`, 1178
quadros, MP4 1080x1920 h264+aac de 39,27 s (4,6 MB), 105 s de render. As imagens
foram geradas localmente porque **não há provedor de imagem configurado** neste
computador (`cover.load_config()` -> `enabled=False`); esse caminho falha alto com
mensagem pedindo a configuração, e tem teste.

### 7.2 Achados fora do escopo (NÃO corrigidos aqui)

Encontrados durante a implementação, em código que este trabalho não podia mexer:

- **edge-tts 7.x não devolve `WordBoundary` por padrão.** A assinatura mudou para
  `boundary='SentenceBoundary'`, e `video_translator.py:241` filtra exatamente
  `chunk["type"] == "WordBoundary"`. Resultado: o sidecar de palavras da dublagem
  fica sempre vazio e a legenda cai silenciosamente nos tempos do Whisper. Para o
  modo História, `voices.py` passa `boundary="WordBoundary"` explicitamente.
- **`pt-BR-ValerioNeural` não existe no serviço** (devolve `NoAudioReceived`), mas
  consta como voz disponível em `video_translator.get_available_voices()`. Quem
  escolher Valério cai no fallback para Antonio sem aviso claro. O modo História
  usa Antonio, Francisca e Thalita, as três que respondem.
- **Não há `ffprobe` neste computador** (só `ffmpeg`, vindo de uma pasta de outro
  projeto no PATH). `publish_export._probe` devolve `{}` em silêncio. O modo
  História não depende de ffprobe: mede duração pelo stderr do ffmpeg
  (`subtitle_timing.parse_ffmpeg_duration`) e confere o MP4 final antes de
  entregá-lo.
- **O lote do YouTube não grava histórico de publicação** (`supo_api.py`), então um
  corte publicado só lá continua "pronto" na Biblioteca e pode ser reenviado. O
  modo História grava um registro por plataforma, com `output_variant_id`, e usa
  esse registro como trava.
