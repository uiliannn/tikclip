"""Modo história: vídeo vertical curto gerado do zero ("o vídeo é código").

Este pacote é um caminho **adicional** de produção. Ele não corta, não dubla e
não traduz nada: parte de um roteiro escrito por LLM, gera as imagens, sintetiza
as falas, descreve a composição num `timeline.json` e renderiza essa composição
num MP4 vertical. O motor de corte/dublagem existente continua intocado.

Peças:
- ``script``    roteiro de ficção (uma chamada de LLM);
- ``voices``    três vozes sintéticas + tempos de palavra do próprio TTS;
- ``images``    seis imagens do provedor já configurado;
- ``builder``   roteiro + áudios + imagens -> ``timeline.json``;
- ``player``    a timeline em HTML/CSS com ``seek(t)``, sem CDN;
- ``render``    Playwright quadro a quadro + FFmpeg (H.264/AAC);
- ``projects``  orquestração e registro no estado do projeto;
- ``publish``   publicação pelos publicadores locais já existentes.
"""
