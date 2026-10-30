@echo off
chcp 65001 > nul
title TikClip - Central de Cortes Inteligentes
cls
echo ========================================================
echo               INICIANDO TIKCLIP WEB                     
echo ========================================================
echo.

cd /d "%~dp0"

IF EXIST ".venv\Scripts\activate.bat" (
    echo [OK] Ativando ambiente virtual .venv...
    call .venv\Scripts\activate.bat
) ELSE (
    echo [INFO] Usando interpretador Python global do sistema...
)

echo [1/2] Iniciando servidor TikClip em http://localhost:8585/ui/ ...
start "" http://localhost:8585/ui/

echo [2/2] Servidor rodando. Para encerrar, feche esta janela.
python server.py

pause
