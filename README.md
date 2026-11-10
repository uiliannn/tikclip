# 🎬 TikClip — Central Inteligente de Cortes, Dublagem, Tradução e Publicação Multi-Plataforma

<p align="center">
  <img src="docs/screenshots/01-home-criar.png" alt="TikClip Web Interface" width="90%" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);" />
</p>

O **TikClip** é uma plataforma *all-in-one* de automação audiovisual e inteligência artificial projetada para criadores de conteúdo, agências e operadores de canais dark. O ecossistema cobre o ciclo completo de produção: desde o download e monitoramento de vídeos, transcrição local via Whisper, tradução e **dublagem neural com voz clonada (VoiceStudio)**, até a geração de legendas dinâmicas e a publicação automatizada no **TikTok** e **YouTube Shorts**.

---

## 📸 Demonstração das Funcionalidades

### 1. 🌐 Tradutor & Dublador de Vídeos com IA (VoiceStudio & Edge-TTS)
- Transcrição precisa com timestamps via **faster-whisper** local.
- Tradução semântica automática para Português (PT-BR).
- Síntese vocal de alta fidelidade conectada diretamente ao motor local do **VoiceStudio (OmniVoice na porta 3900)** com suporte a perfis clonados (como **Davy Jones**, vozes personalizadas ou vozes neurais brasileiras).
- Mixagem sonora profissional com **Ducking ajustável** (preserva a trilha sonora original de fundo em volume reduzido enquanto a voz dublada fala).
- Sistema flexível de legendas: botão para desabilitar legendas, 6 estilos visuais (Amarelo Viral TikTok, Branco Clássico, Verde Neon, Vermelho Gamer, Ciano, Box Preto), controle de tamanho, alinhamento na tela e opção de forçar **MAIÚSCULAS**.
- Galeria responsiva com cards compactos em proporção 9:16 (sem distorção ou excesso de tamanho).

### 2. 📥 Downloader Integrado do TikTok
- Baixe diretamente pela interface web colando links de **vídeos individuais**, perfis de criadores (`@usuario`) ou páginas de **Trending Topics / Inspiração**.
- Extração automática de metadados, descrições e hashtags em arquivos `.info.txt` e capas nítidas em `.jpg`.
- Barra de progresso com porcentagem e velocidade em tempo real.
- Botão direto para enviar o vídeo recém-baixado imediatamente para o Tradutor & Dublador.

### 3. 🔴 Publicador Automático YouTube Studio (Shorts e Vídeos Longos)
- Automação local via navegador com **Playwright** — sem necessidade de chaves de API pagas ou limites da Google Cloud Console.
- Suporte a múltiplos canais do YouTube com alternância de perfis.
- **Conversor Vertical Automático (FFmpeg):** Converte vídeos horizontais (ex: 4:3 ou 16:9) em 9:16 vertical com fundo desfocado (*blurred background*), garantindo que sejam indexados como **YouTube Shorts**.
- Superação automática de diálogos de checagem do YouTube Studio (*"Ainda estamos verificando seu conteúdo" -> "Publicar mesmo assim"*).
- Limpeza preventiva de dados temporários e cache do navegador (*IndexedDB blobs*) para manter o disco sempre livre.

### 4. 📱 Publicador TikTok Multi-Contas
- Gerencie múltiplas contas do TikTok com perfis de navegação completamente isolados.
- Publicação individual ou em lote com intervalos configuráveis entre postagens para evitar bloqueios (*shadowban*).
- Extração de capas em alta definição com FFmpeg para exibição no feed.

### 5. 🤖 Motor Duplo de IA (Google Gemini + DeepSeek)
- Chave seletora em tempo real para alternar entre **Google Gemini** (Gemini 2.5 Flash / Flash Lite) e **DeepSeek** (DeepSeek V3 Chat).
- Análise semântica inteligente de vídeos longos com identificação automática dos momentos de maior retenção e impacto emocional (*viral hooks*).

### 6. 📹 Monitoramento Passivo de Canais do YouTube
- Monitore canais inteiros pelo `@handle`.
- Assim que um novo vídeo for publicado no canal monitorado, o TikClip baixa, gera os cortes virais e programa a postagem na conta vinculada.

### 7. ⚡ Servidor MCP & API REST para Agentes Autônomos
- Servidor **Model Context Protocol (MCP)** integrado para conexão com **Cursor**, **Claude Desktop**, **Antigravity** ou agentes autônomos locais.
- Documentação interativa Swagger/OpenAPI disponível em `/docs`.

---

## ⚡ Principais Tecnologias e Bibliotecas

| Área | Tecnologias Utilizadas |
| :--- | :--- |
| **Backend** | Python 3.10+, FastAPI, Uvicorn, SQLite, Asyncio |
| **Frontend** | HTML5 Semântico, CSS3 Moderno (Glassmorphism), Vanilla JavaScript reativo |
| **Processamento de Mídia** | FFmpeg 6.0+, Libx264, AAC, filtros de legendas ASS/SRT, filtros de mixagem de áudio |
| **Inteligência Artificial** | faster-whisper, Google Gemini API, DeepSeek API, Google Translate API |
| **Síntese de Voz (TTS)** | VoiceStudio (OmniVoice local porta 3900), Edge-TTS Neural (Microsoft) |
| **Automação Web** | Playwright (Chromium headless e headed) |
| **Downloads** | yt-dlp, TikWM API fallback, curl_cffi |

---

## 🚀 Instalação Rápida

### Opção A: Execução em 1 Clique (Windows)
Se você já possui o ambiente configurado, basta clicar duas vezes no atalho:
```text
Iniciar TikClip.bat
```
Ou executar no terminal:
```powershell
python server.py
```
O servidor iniciará automaticamente em **[http://localhost:8585/ui/](http://localhost:8585/ui/)**.

---

### Opção B: Instalação Manual Passo a Passo

#### 1. Pré-requisitos
- **Python 3.10** ou superior instalado.
- **FFmpeg** instalado e adicionado ao `PATH` do sistema:
  - Windows: `winget install Gyan.FFmpeg`
  - Linux (Ubuntu/Debian): `sudo apt update && sudo apt install -y ffmpeg`
  - macOS: `brew install ffmpeg`

#### 2. Clonar o Repositório
```bash
git clone https://github.com/uiliannn/tikclip.git
cd tikclip
```

#### 3. Criar e Ativar o Ambiente Virtual
- **Windows:**
  ```powershell
  python -m venv .venv
  .venv\Scripts\activate
  ```
- **Linux/macOS:**
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```

#### 4. Instalar Dependências e Navegadores do Playwright
```bash
pip install -r requirements.txt
playwright install chromium
```

#### 5. Configurar o Arquivo de Ambiente (.env)
Copie o modelo `.env.example`:
```bash
cp .env.example .env
```
*(Você pode preencher suas chaves do Gemini e DeepSeek no `.env` ou salvá-las diretamente pela interface web na aba **Configurações**).*

#### 6. Executar o TikClip
```bash
python server.py
```
Acesse em seu navegador:
👉 **[http://localhost:8585/ui/](http://localhost:8585/ui/)**

---

## 📁 Estrutura do Projeto

```text
tikclip/
├── backend/                      # Núcleo da API e regras de negócio
│   ├── api/                      # Rotas e controladores da API FastAPI
│   ├── core/                     # Gerenciamento de tarefas, SQLite e IA
│   ├── pipeline/                 # Processamento de vídeo, corte e legendagem
│   └── services/                 # Serviços de suporte e integrações
├── docs/                         # Documentação e capturas de tela da interface
├── web/                          # Interface Web (Single Page Application)
│   ├── css/                      # Folhas de estilo da UI
│   ├── js/                       # supoclip.js (Controlador principal da interface)
│   └── index.html                # Ponto de entrada do painel de controle
├── channel_monitor.py            # Monitor contínuo de canais do YouTube
├── tiktok_library.py             # Gerenciamento de mídias e criadores do TikTok
├── tiktok_downloader.py          # Módulo de download com yt-dlp e fallback
├── video_translator.py           # Pipeline de dublagem, Whisper, VoiceStudio e legendas
├── youtube_local_publisher.py    # Publicador autônomo do YouTube Studio com Playwright
├── supo_api.py                   # Roteador central de endpoints TikClip
├── server.py                     # Inicializador do servidor HTTP/ASGI
├── Iniciar TikClip.bat           # Launcher de um clique para Windows
├── requirements.txt              # Dependências do projeto
└── README.md                     # Documentação completa do projeto
```

---

## 🔒 Privacidade e Segurança Local
- O projeto opera prioritariamente em ambiente local: suas sessões do navegador, cookies do TikTok e YouTube, perfis de voz do VoiceStudio e vídeos processados permanecem exclusivamente na sua máquina (`%APPDATA%/TikClip`).
- Nenhuma credencial ou mídia é compartilhada com servidores de terceiros não configurados por você.

---

## 📄 Licença
Distribuído sob a licença **MIT**. Consulte o arquivo `LICENSE` para mais detalhes.
