# 🤖 Prompt de Instalação e Execução Automática (Para Google Gemini / Agentes IA)

> **Como usar este prompt:**  
> Copie todo o bloco de instruções abaixo e envie diretamente para o **Google Gemini**, **Cursor**, **Claude** ou qualquer assistente de código no seu terminal/IDE. A inteligência artificial fará todo o processo de preparação, instalação e inicialização por você.

---

```markdown
Você é um Engenheiro de Software Especialista em Python e Automação Web.
Sua missão é instalar e colocar em funcionamento o projeto **TikClip** neste computador a partir deste repositório.

Por favor, siga rigorosamente os passos abaixo em ordem:

### 1. Diagnóstico do Ambiente
- Verifique se o Python (versão 3.10 ou superior) está instalado no sistema (`python --version`).
- Verifique se o gerenciador de pacotes `pip` está atualizado.
- Verifique se o **FFmpeg** está instalado e disponível no PATH (`ffmpeg -version`). Caso não esteja disponível no PATH:
  - No Windows, você pode instalá-lo via winget (`winget install Gyan.FFmpeg`) ou sugerir/baixar o binário estático.
  - No Linux, instale via `sudo apt update && sudo apt install -y ffmpeg`.
  - No macOS, instale via `brew install ffmpeg`.

### 2. Criação do Ambiente Virtual (Virtualenv)
- Crie um ambiente virtual isolado `.venv` na raiz do projeto:
  `python -m venv .venv`
- Ative o ambiente virtual:
  - Windows: `.venv\Scripts\activate`
  - Linux/macOS: `source .venv/bin/activate`

### 3. Instalação das Dependências
- Instale todas as bibliotecas listadas no `requirements.txt`:
  `pip install -r requirements.txt`
- Instale os navegadores do Playwright (utilizados pelo Publicador do TikTok):
  `playwright install chromium`

### 4. Configuração das Variáveis (.env)
- Se o arquivo `.env` não existir, crie-o a partir do `.env.example`:
  - Copie `.env.example` para `.env`.
- Informe ao usuário que ele pode configurar suas chaves de API (Google Gemini e DeepSeek) diretamente pelo painel Web em **Configurações**, ou inseri-las diretamente no arquivo `.env`.

### 5. Inicialização do Servidor TikClip
- Execute o arquivo principal do servidor:
  `python server.py`
- Confirme que a aplicação subiu com sucesso na porta 8585.
- Abra ou informe o link no navegador para o usuário:
  `http://localhost:8585/ui/`

Se ocorrer qualquer erro durante a instalação ou inicialização, explique de forma clara e objetiva o motivo e aplique a correção automaticamente.
```
