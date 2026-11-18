"""
Live Clipping (inspirado no PeakCut):
Grava lives do YouTube, Twitch ou Kick em buffer circular (.ts a cada 6s),
analisa o volume do áudio (dB) em tempo real para detectar picos de hype,
e gera cortes automáticos ou manuais (> 60s) enviando direto ao pipeline do TikClip.
"""
from __future__ import annotations

import collections
import datetime
import logging
import math
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger('tikclip.live_clipper')

SEG_SEC = 6              # Duração de cada segmento do buffer circular (s)
MAX_BUFFER_SEGS = 180    # ~18 minutos de buffer circular no disco
MIN_CLIP_SEC = 65        # Sempre > 60s para monetização no TikTok
DEFAULT_CLIP_SEC = 85
PEAK_COOLDOWN_SEC = 120  # Intervalo mínimo entre cortes automáticos por pico
PEAK_POST_WAIT_SEC = 22  # Espera após o pico para capturar a reação completa
PEAK_DELTA_DB = 7.5      # Salto mínimo acima da mediana recente para considerar pico
PEAK_MIN_DB = -26.0      # Nível mínimo absoluto (dBFS) para evitar disparo no silêncio
SR = 16000               # Taxa de amostragem do pipe de áudio (16 kHz mono s16le)


class LiveResolveError(RuntimeError):
    pass


def _data_dir() -> Path:
    base = os.environ.get('TIKCLIP_DATA_DIR') or os.environ.get('APPDATA') or str(Path.home())
    p = Path(base)
    if p.name != 'TikClip':
        p = p / 'TikClip'
    return p


def _ffmpeg_bin() -> str:
    env_bin = os.environ.get('FFMPEG_BIN')
    if env_bin and Path(env_bin).is_file():
        return env_bin
    studio_ff = Path(r'C:\Users\Uilian\Desktop\tikclip-studio\runtime\ffmpeg\ffmpeg.exe')
    if studio_ff.is_file():
        return str(studio_ff)
    return shutil.which('ffmpeg') or 'ffmpeg'


def _creation_flags() -> int:
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if os.name == 'nt' else 0


def normalize_live_url(url: str) -> str:
    u = (url or '').strip()
    if not u:
        return ''
    if not re.match(r'^https?://', u, re.IGNORECASE):
        u = 'https://' + u
    # Canal YouTube -> /live
    m = re.match(
        r'^(https?://(?:www\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+))/?(?:[?#].*)?$',
        u,
        re.IGNORECASE,
    )
    if m:
        return m.group(1) + '/live'
    return u


def detect_platform(url: str) -> str:
    low = url.lower()
    if 'twitch.tv' in low:
        return 'Twitch'
    if 'kick.com' in low:
        return 'Kick'
    if 'youtube.com' in low or 'youtu.be' in low:
        return 'YouTube'
    return 'Stream'


def resolve_live_stream(url: str) -> dict[str, Any]:
    """Usa yt-dlp para validar se a URL está ao vivo e obter stream reproduzível."""
    import yt_dlp

    norm_url = normalize_live_url(url)
    if not norm_url:
        raise LiveResolveError('Informe uma URL de live válida.')

    platform = detect_platform(norm_url)
    base_opts: dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'format': 'b[height<=720]/bv*[height<=720]+ba/b',
    }
    if shutil.which('node'):
        base_opts['js_runtimes'] = {'node': {}}

    attempts: list[dict[str, Any]] = []
    if platform == 'YouTube':
        attempts.append({**base_opts})
        attempts.append({
            **base_opts,
            'extractor_args': {'youtube': {'player_client': ['android_vr', 'tv', 'web']}},
        })
        cookies_file = _data_dir() / 'cookies-youtube.txt'
        if cookies_file.is_file():
            attempts.append({**base_opts, 'cookiefile': str(cookies_file)})
    else:
        attempts.append(base_opts)

    info = None
    last_exc: Exception | None = None
    for opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(norm_url, download=False)
            if info:
                break
        except Exception as exc:
            last_exc = exc
            low = str(exc).lower()
            if 'premieres in' in low or 'live event will begin' in low:
                raise LiveResolveError('Esta transmissão ainda está agendada e não começou.') from exc
            if 'not live' in low or 'offline' in low:
                raise LiveResolveError(f'Este canal ({platform}) não está ao vivo no momento.') from exc
            continue

    if not info:
        msg = str(last_exc) if last_exc else 'Sem resposta do yt-dlp'
        raise LiveResolveError(f'Não foi possível abrir a live: {msg[-220:]}')

    if not info or not info.get('is_live'):
        raise LiveResolveError(f'Este canal/vídeo ({platform}) não está ao vivo no momento.')

    fmts = info.get('requested_formats') or [info]
    stream_urls = [f['url'] for f in fmts if f.get('url')]
    if not stream_urls:
        raise LiveResolveError('yt-dlp não retornou URL de stream reproduzível.')

    title = str(info.get('title') or '').strip()
    if '(live)' in title.lower() and info.get('description'):
        title = str(info['description']).splitlines()[0][:120]
    uploader = str(info.get('uploader') or info.get('channel') or platform).strip()

    return {
        'url': norm_url,
        'platform': platform,
        'stream_urls': stream_urls,
        'title': title or f'Live de {uploader}',
        'uploader': uploader,
    }


def _pcm_rms_db(pcm_bytes: bytes) -> float:
    """Calcula RMS em dBFS de um bloco PCM 16-bit signed mono."""
    n = len(pcm_bytes) // 2
    if n <= 0:
        return -60.0
    mv = memoryview(pcm_bytes).cast('h')
    acc = 0.0
    # Amostra a cada 4 samples para velocidade em Python puro (~4000 ops/s)
    step = 4
    count = 0
    for i in range(0, n, step):
        v = mv[i]
        acc += v * v
        count += 1
    if count == 0:
        return -60.0
    rms = math.sqrt(acc / count) / 32768.0
    if rms <= 1e-6:
        return -60.0
    return round(max(-60.0, 20.0 * math.log10(rms)), 2)


class LiveSession:
    def __init__(
        self,
        session_id: str,
        info: dict[str, Any],
        clip_mode: str = 'peak',
        clip_duration: int = DEFAULT_CLIP_SEC,
        interval_sec: int = 180,
        send_to_pipeline: bool = True,
        server_port: int = 8585,
    ):
        self.id = session_id
        self.url = info['url']
        self.platform = info['platform']
        self.stream_urls = info['stream_urls']
        self.title = info['title']
        self.uploader = info['uploader']

        self.clip_mode = clip_mode if clip_mode in ('peak', 'interval', 'manual') else 'peak'
        self.clip_duration = max(MIN_CLIP_SEC, min(180, int(clip_duration or DEFAULT_CLIP_SEC)))
        self.interval_sec = max(90, min(600, int(interval_sec or 180)))
        self.send_to_pipeline = bool(send_to_pipeline)
        self.server_port = server_port

        self.dir = _data_dir() / 'live_sessions' / session_id
        self.buf_dir = self.dir / 'buffer'
        self.clips_dir = self.dir / 'clips'
        self.buf_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)

        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.status = 'recording'  # recording | stopped | error
        self.error: str | None = None

        self.proc: subprocess.Popen | None = None
        self.db_history: collections.deque[float] = collections.deque(maxlen=180)
        self.current_db: float = -60.0
        self.baseline_db: float = -45.0
        self.last_peak_at: float = 0.0
        self.last_auto_clip_at: float = 0.0
        self.pending_peak_timer: float | None = None
        self.clip_seq = 0
        self.clips: list[dict[str, Any]] = []

    def elapsed_sec(self) -> int:
        end = self.ended_at if self.ended_at else time.time()
        return max(0, int(end - self.started_at))

    def _list_segments(self) -> list[Path]:
        if not self.buf_dir.is_dir():
            return []
        segs = sorted(self.buf_dir.glob('seg_*.ts'))
        return segs

    def _cleanup_old_segments(self) -> None:
        segs = self._list_segments()
        if len(segs) > MAX_BUFFER_SEGS:
            for old in segs[: len(segs) - MAX_BUFFER_SEGS]:
                try:
                    old.unlink(missing_ok=True)
                except Exception:
                    pass

    def buffer_seconds(self) -> int:
        # Ignora o último segmento ainda sendo escrito pelo ffmpeg quando gravando
        segs = self._list_segments()
        usable = max(0, len(segs) - (1 if self.status == 'recording' else 0))
        return usable * SEG_SEC

    def start(self) -> None:
        t = threading.Thread(target=self._run_ffmpeg, name=f'live-{self.id}', daemon=True)
        t.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.status == 'recording':
                self.status = 'stopped'
                self.ended_at = time.time()
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _run_ffmpeg(self) -> None:
        ff = _ffmpeg_bin()
        cmd = [ff, '-y', '-hide_banner', '-loglevel', 'warning']
        for u in self.stream_urls:
            cmd.extend(['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5', '-i', u])

        seg_pattern = str(self.buf_dir / 'seg_%05d.ts')
        if len(self.stream_urls) >= 2:
            cmd.extend(['-map', '0:v:0', '-map', '1:a:0'])
        cmd.extend([
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-f', 'segment',
            '-segment_time', str(SEG_SEC),
            '-reset_timestamps', '1',
            seg_pattern,
        ])
        # Segundo output: pipe de áudio 16kHz mono para medir loudness (dB) em tempo real
        if len(self.stream_urls) >= 2:
            cmd.extend(['-map', '1:a:0'])
        else:
            cmd.extend(['-map', '0:a:0?'])
        cmd.extend(['-vn', '-ac', '1', '-ar', str(SR), '-f', 's16le', 'pipe:1'])

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_creation_flags(),
            )
            with self.lock:
                self.proc = proc
        except Exception as exc:
            with self.lock:
                self.status = 'error'
                self.error = f'Falha ao iniciar FFmpeg: {exc}'
                self.ended_at = time.time()
            return

        # Thread para drenar stderr sem travar o pipe
        stderr_tail: collections.deque[str] = collections.deque(maxlen=20)

        def _drain_stderr():
            if not proc.stderr:
                return
            for raw in proc.stderr:
                line = raw.decode('utf-8', errors='replace').strip()
                if line:
                    stderr_tail.append(line)

        threading.Thread(target=_drain_stderr, daemon=True).start()

        bytes_per_sec = SR * 2
        buf = bytearray()
        last_cleanup = time.time()

        try:
            while not self.stop_event.is_set() and proc.stdout:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= bytes_per_sec:
                    sec_bytes = bytes(buf[:bytes_per_sec])
                    del buf[:bytes_per_sec]
                    db = _pcm_rms_db(sec_bytes)
                    self._on_second_db(db)

                now = time.time()
                if now - last_cleanup > 15:
                    self._cleanup_old_segments()
                    last_cleanup = now
        except Exception as exc:
            logger.warning(f'[Live {self.id}] Erro lendo pipe: {exc}')
        finally:
            rc = proc.wait()
            with self.lock:
                if not self.stop_event.is_set():
                    if rc != 0 and self.elapsed_sec() < 10:
                        self.status = 'error'
                        self.error = ' | '.join(stderr_tail) or f'FFmpeg encerrou (código {rc}).'
                    else:
                        self.status = 'stopped'
                    self.ended_at = time.time()

    def _on_second_db(self, db: float) -> None:
        now = time.time()
        trigger_reason: str | None = None
        with self.lock:
            self.current_db = db
            self.db_history.append(db)
            if len(self.db_history) >= 15:
                recent = list(self.db_history)[-60:]
                self.baseline_db = round(statistics.median(recent), 2)

            buf_sec = self.buffer_seconds()
            if self.clip_mode == 'peak' and buf_sec >= MIN_CLIP_SEC:
                # Detecta pico se ainda não há pico pendente e passou o cooldown
                if (
                    self.pending_peak_timer is None
                    and (now - self.last_auto_clip_at) >= PEAK_COOLDOWN_SEC
                    and db >= PEAK_MIN_DB
                    and (db - self.baseline_db) >= PEAK_DELTA_DB
                ):
                    self.last_peak_at = now
                    self.pending_peak_timer = now + PEAK_POST_WAIT_SEC
                    logger.info(f'[Live {self.id}] Pico detectado! {db} dB (base {self.baseline_db} dB)')

                if self.pending_peak_timer is not None and now >= self.pending_peak_timer:
                    self.pending_peak_timer = None
                    self.last_auto_clip_at = now
                    trigger_reason = f'Pico de Áudio ({db:.1f} dB)'

            elif self.clip_mode == 'interval' and buf_sec >= MIN_CLIP_SEC:
                ref = self.last_auto_clip_at or self.started_at
                if (now - ref) >= self.interval_sec:
                    self.last_auto_clip_at = now
                    trigger_reason = f'Intervalo ({self.interval_sec}s)'

        if trigger_reason:
            threading.Thread(
                target=self.create_clip,
                kwargs={'seconds': self.clip_duration, 'trigger': trigger_reason},
                daemon=True,
            ).start()

    def create_clip(self, seconds: int = DEFAULT_CLIP_SEC, trigger: str = 'Manual') -> dict[str, Any]:
        target_sec = max(MIN_CLIP_SEC, min(180, int(seconds or self.clip_duration)))
        with self.lock:
            segs = self._list_segments()
            if self.status == 'recording' and len(segs) > 1:
                # Exclui o último segmento ainda aberto pelo ffmpeg
                segs = segs[:-1]
            if not segs:
                raise RuntimeError('Buffer ainda está vazio. Aguarde alguns segundos de gravação.')

            needed_segs = max(1, math.ceil(target_sec / SEG_SEC))
            chosen = segs[-needed_segs:]
            actual_sec = len(chosen) * SEG_SEC
            if actual_sec < 61:
                raise RuntimeError(
                    f'Buffer atual tem {actual_sec}s. Aguarde pelo menos 65s de live para gerar cortes > 60s.'
                )

            self.clip_seq += 1
            seq = self.clip_seq
            ts_tag = datetime.datetime.now().strftime('%H%M%S')
            out_name = f'live_cut_{seq:03d}_{ts_tag}.mp4'
            out_path = self.clips_dir / out_name
            concat_file = self.clips_dir / f'concat_{seq:03d}.txt'

        lines = [f"file '{p.resolve().as_posix()}'" for p in chosen]
        concat_file.write_text('\n'.join(lines), encoding='utf-8')

        ff = _ffmpeg_bin()
        cmd = [
            ff, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'concat', '-safe', '0', '-i', str(concat_file),
            '-c', 'copy', '-movflags', '+faststart', str(out_path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, creationflags=_creation_flags())
            if res.returncode != 0 or not out_path.is_file():
                raise RuntimeError(res.stderr.strip() or 'Falha ao montar vídeo MP4 do buffer.')
        finally:
            try:
                concat_file.unlink(missing_ok=True)
            except Exception:
                pass

        clip_info: dict[str, Any] = {
            'seq': seq,
            'filename': out_name,
            'duration_sec': actual_sec,
            'trigger': trigger,
            'created_at': datetime.datetime.now().strftime('%H:%M:%S'),
            'size_mb': round(out_path.stat().st_size / (1024 * 1024), 2),
            'project_id': None,
            'pipeline_status': 'pendente' if self.send_to_pipeline else 'salvo',
            'download_url': f'/api/v1/supo/live/{self.id}/clips/{out_name}',
        }

        with self.lock:
            self.clips.insert(0, clip_info)

        if self.send_to_pipeline:
            try:
                proj_name = f'[LIVE] {self.uploader[:40]} #{seq}'
                import_url = f'http://127.0.0.1:{self.server_port}/api/v1/studio/import'
                with out_path.open('rb') as f:
                    files = {'video': (out_name, f, 'video/mp4')}
                    data = {
                        'goal': 'auto',
                        'language': 'source',
                        'aspect': 'portrait',
                        'duration': str(min(120, max(65, actual_sec))),
                        'platforms': 'tiktok',
                        'auto_start': 'true',
                        'brand_outro_enabled': 'false',
                        'name': proj_name,
                    }
                    r = httpx.post(import_url, data=data, files=files, timeout=60.0)
                if r.status_code == 200:
                    j = r.json()
                    clip_info['project_id'] = j.get('project_id') or j.get('id')
                    clip_info['pipeline_status'] = 'enviado'
                else:
                    clip_info['pipeline_status'] = f'erro ({r.status_code})'
            except Exception as exc:
                logger.warning(f'[Live {self.id}] Falha ao enviar para pipeline: {exc}')
                clip_info['pipeline_status'] = 'erro_envio'

        return clip_info

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                'id': self.id,
                'url': self.url,
                'platform': self.platform,
                'title': self.title,
                'uploader': self.uploader,
                'status': self.status,
                'error': self.error,
                'clip_mode': self.clip_mode,
                'clip_duration': self.clip_duration,
                'interval_sec': self.interval_sec,
                'send_to_pipeline': self.send_to_pipeline,
                'elapsed_sec': self.elapsed_sec(),
                'buffer_sec': self.buffer_seconds(),
                'current_db': self.current_db,
                'baseline_db': self.baseline_db,
                'db_history': list(self.db_history)[-60:],
                'last_peak_at': self.last_peak_at,
                'pending_peak': self.pending_peak_timer is not None,
                'clips_count': len(self.clips),
                'clips': list(self.clips)[:30],
            }


class LiveManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, LiveSession] = {}

    def start_session(
        self,
        url: str,
        clip_mode: str = 'peak',
        clip_duration: int = DEFAULT_CLIP_SEC,
        interval_sec: int = 180,
        send_to_pipeline: bool = True,
        server_port: int = 8585,
    ) -> dict[str, Any]:
        info = resolve_live_stream(url)
        sid = uuid.uuid4().hex[:10]
        sess = LiveSession(
            session_id=sid,
            info=info,
            clip_mode=clip_mode,
            clip_duration=clip_duration,
            interval_sec=interval_sec,
            send_to_pipeline=send_to_pipeline,
            server_port=server_port,
        )
        with self._lock:
            self._sessions[sid] = sess
        sess.start()
        return sess.to_dict()

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._sessions.values())
        items.sort(key=lambda s: s.started_at, reverse=True)
        return [s.to_dict() for s in items]

    def get_session(self, sid: str) -> LiveSession | None:
        with self._lock:
            return self._sessions.get(sid)

    def stop_session(self, sid: str) -> dict[str, Any]:
        sess = self.get_session(sid)
        if not sess:
            raise KeyError('Sessão de live não encontrada.')
        sess.stop()
        return sess.to_dict()

    def clip_now(self, sid: str, seconds: int = DEFAULT_CLIP_SEC) -> dict[str, Any]:
        sess = self.get_session(sid)
        if not sess:
            raise KeyError('Sessão de live não encontrada.')
        return sess.create_clip(seconds=seconds, trigger='Manual')


live_manager = LiveManager()
