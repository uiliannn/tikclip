"""Página HTML do modo história: a composição em CSS/JS, sem GSAP nem CDN.

O app roda offline, então não há biblioteca de animação externa: a página expõe
uma função `seek(t)` que posiciona tudo (cena, zoom, transição, legenda, barra)
a partir do tempo `t` da timeline. O renderizador chama `seek` frame a frame e
captura o resultado; o mesmo arquivo, aberto no navegador com `?preview`, toca
a animação em tempo real.

Como não há `transition`/`animation` de CSS em lugar nenhum, cada quadro é
determinístico: o mesmo `t` produz sempre o mesmo pixel.
"""
from __future__ import annotations

import base64
import html
from pathlib import Path

TEMPLATE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>__TITLE_TEXT__</title>
<style>
html,body{margin:0;padding:0;background:#000;overflow:hidden}
#stage{position:relative;width:__W__px;height:__H__px;overflow:hidden;background:#000;
  font-family:"__FONT__","Segoe UI",system-ui,Arial,sans-serif}
.scene{position:absolute;inset:0}
.scene img{width:100%;height:100%;object-fit:cover;display:block;
  transform:translateZ(0)}
#shade{position:absolute;left:0;right:0;bottom:0;height:42%;pointer-events:none;
  background:linear-gradient(to top,rgba(0,0,0,.82) 0%,rgba(0,0,0,.35) 55%,rgba(0,0,0,0) 100%)}
#vignette{position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(ellipse at center,rgba(0,0,0,0) 55%,rgba(0,0,0,.45) 100%)}
#title{position:absolute;left:64px;right:64px;top:150px;text-align:center;color:#fff;
  font-size:76px;font-weight:800;line-height:1.08;letter-spacing:-1px;
  text-shadow:0 4px 18px rgba(0,0,0,.75);pointer-events:none}
#cueband{position:absolute;left:64px;right:64px;top:0;height:100%;display:flex;
  align-items:center;justify-content:center;pointer-events:none}
#cue{color:__TEXT_COLOR__;font-size:__CAPTION_SIZE__px;font-weight:800;line-height:1.16;
  text-align:center;white-space:pre-wrap;__CAPTION_SHADOW__}
#barTrack{position:absolute;left:0;right:0;bottom:0;height:10px;background:rgba(255,255,255,.16)}
#bar{height:100%;width:0;background:__ACCENT__}
</style></head>
<body>
<div id="stage">
  <div id="sceneLayer"></div>
  <div id="shade"></div>
  <div id="vignette"></div>
  <div id="title">__TITLE_TEXT__</div>
  <div id="cueband" style="transform:translateY(__CUE_SHIFT__px)"><div id="cue"></div></div>
  <div id="barTrack"><div id="bar"></div></div>
</div>
<script>
"use strict";
const TL = __PAYLOAD__;
const DURATION = TL.duration;
const PREVIEW = new URLSearchParams(location.search).has("preview");
const layer = document.getElementById("sceneLayer");
const cueEl = document.getElementById("cue");
const titleEl = document.getElementById("title");
const barEl = document.getElementById("bar");

const ORIGINS = { center: "center", top: "center top", bottom: "center bottom" };

const scenes = TL.scenes.map((data) => {
  const holder = document.createElement("div");
  holder.className = "scene";
  const img = document.createElement("img");
  img.src = data.image;
  img.style.transformOrigin = ORIGINS[data.ken_burns.focus] || "center";
  holder.appendChild(img);
  layer.appendChild(holder);
  return { data, holder, img };
});

function clamp01(value) { return value < 0 ? 0 : (value > 1 ? 1 : value); }
function smooth(value) { const x = clamp01(value); return x * x * (3 - 2 * x); }

function seek(t) {
  t = Math.max(0, Math.min(DURATION, t));
  for (const item of scenes) {
    const s = item.data;
    const length = s.end - s.start;
    const local = t - s.start;
    let opacity = (local < 0 || local > length) ? 0 : 1;
    const fade = s.transition.type === "fade" ? s.transition.duration / 2 : 0;
    if (fade > 0 && local >= 0 && local <= length) {
      const entering = smooth(local / fade);
      const leaving = smooth((length - local) / fade);
      opacity = Math.min(entering, leaving);
    }
    opacity = clamp01(opacity);
    item.holder.style.opacity = opacity.toFixed(4);
    item.holder.style.visibility = opacity <= 0.0015 ? "hidden" : "visible";
    const progress = length > 0 ? clamp01(local / length) : 0;
    const kb = s.ken_burns;
    const scale = kb.start_scale + (kb.end_scale - kb.start_scale) * progress;
    item.img.style.transform = "scale(" + scale.toFixed(5) + ")";
  }
  const active = TL.cues.filter((cue) => t >= cue.start && t < cue.end)[0];
  cueEl.textContent = active ? active.text : "";
  cueEl.style.opacity = active ? "1" : "0";
  const visible = t < TL.title_duration
    ? smooth(t / 0.35) * smooth((TL.title_duration - t) / 0.55) : 0;
  titleEl.style.opacity = clamp01(visible).toFixed(4);
  titleEl.style.visibility = visible <= 0.0015 ? "hidden" : "visible";
  barEl.style.width = (clamp01(t / DURATION) * 100).toFixed(3) + "%";
}

window.seek = seek;

function play() {
  const started = performance.now();
  function step(now) {
    const t = (now - started) / 1000;
    seek(t);
    if (t < DURATION) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

const decoding = Array.from(document.images).map((img) => {
  if (img.complete) return Promise.resolve();
  return new Promise((resolve) => { img.onload = resolve; img.onerror = resolve; });
});
Promise.all(decoding).then(() => {
  seek(0);
  window.__ready = true;
  if (PREVIEW) play();
});
</script>
</body></html>
"""

TITLE_SECONDS = 2.6


def data_uri(path: Path) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    mime = 'image/png' if suffix == '.png' else 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def payload(timeline, project_dir: Path) -> dict:
    """Somente o que a página precisa: tempos, imagens embutidas e legendas."""
    project_dir = Path(project_dir)
    scenes = []
    for scene in timeline.scenes:
        image = project_dir / scene.image
        if not image.is_file():
            raise FileNotFoundError(f'Imagem da cena {scene.index + 1} não encontrada: {image}')
        scenes.append({
            'start': round(scene.start, 3),
            'end': round(scene.end, 3),
            'image': data_uri(image),
            'transition': {'type': scene.transition.type,
                           'duration': round(scene.transition.duration, 3)},
            'ken_burns': {'start_scale': scene.ken_burns.start_scale,
                          'end_scale': scene.ken_burns.end_scale,
                          'focus': scene.ken_burns.focus},
        })
    return {
        'duration': timeline.duration,
        'title_duration': round(min(TITLE_SECONDS, timeline.duration / 3), 3),
        'scenes': scenes,
        'cues': [{'start': round(cue.start, 3), 'end': round(cue.end, 3), 'text': cue.text}
                 for cue in timeline.cues],
    }


def render_html(timeline, project_dir: Path) -> str:
    """HTML completo e autossuficiente (imagens embutidas, nenhum arquivo externo)."""
    import json

    style = timeline.style
    shadow = ('text-shadow:0 4px 16px rgba(0,0,0,.85),0 0 2px rgba(0,0,0,.9);'
              if style.shadow else '')
    cue_shift = round((style.caption_y - .5) * timeline.height, 1)
    title_text = html.escape(timeline.title)
    return (TEMPLATE
            .replace('__TITLE_TEXT__', title_text)
            .replace('__W__', str(timeline.width))
            .replace('__H__', str(timeline.height))
            .replace('__FONT__', html.escape(style.font))
            .replace('__TEXT_COLOR__', style.text_color)
            .replace('__ACCENT__', style.accent)
            .replace('__CAPTION_SIZE__', str(style.caption_size))
            .replace('__CAPTION_SHADOW__', shadow)
            .replace('__CUE_SHIFT__', str(cue_shift))
            .replace('__PAYLOAD__', json.dumps(payload(timeline, project_dir),
                                               ensure_ascii=False)))
