/* Tela "História" — vídeo vertical curto gerado do zero.
 *
 * Roteiro (LLM) -> 6 imagens -> 3 vozes TTS -> timeline.json -> render.
 * Não corta, não dubla e não traduz nada: é um caminho adicional de produção.
 *
 * Este arquivo é carregado antes de supoclip.js e expõe `renderHistoria(main)`,
 * que o `navegar()` de supoclip.js chama. Toda a comunicação é com /api/v1/ext/fiction/*.
 */
'use strict';

const HISTORIA_API = '/api/v1/ext';

const historiaState = {
  projectId: null,
  build: null,
  timer: null,
  pubTimer: null,
  platforms: { tiktok: true, youtube: true },
};

function limparTimersHistoria() {
  if (historiaState.timer) { clearInterval(historiaState.timer); historiaState.timer = null; }
  if (historiaState.pubTimer) { clearInterval(historiaState.pubTimer); historiaState.pubTimer = null; }
}

async function apiHistoria(path, opts = {}) {
  const res = await fetch(HISTORIA_API + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch { /* resposta sem JSON */ }
    throw new Error(String(detail));
  }
  return res.json();
}

function historiaSegundos(valor) {
  const total = Math.round(Number(valor) || 0);
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

function historiaRotulo(status) {
  return ({
    building: 'gerando',
    ready: 'pronto',
    failed: 'falhou',
    idle: 'sem história',
  })[status] || status || 'sem história';
}

function renderHistoria(container) {
  limparTimersHistoria();
  const wrap = h('div', { class: 'sc-create-wrap' });

  wrap.append(
    h('h1', { class: 'sc-page-title', text: 'História' }),
    h('p', {
      class: 'sc-page-desc',
      text: 'Gera um vídeo vertical de 30 a 45 s do zero: roteiro de ficção, seis imagens, três vozes sintéticas e legenda queimada. Não usa nenhum vídeo seu como material.',
    }),
  );

  // ---------------------------------------------------------------- formulário
  const tema = h('textarea', {
    class: 'sc-textarea',
    rows: '2',
    placeholder: 'Ex.: uma estrada à noite e um carro que não deveria estar ligado',
  });
  const paleta = h('select', { class: 'sc-select' },
    ...['azure', 'amber', 'coral', 'mint', 'lemon', 'rose', 'lilac'].map((nome) =>
      h('option', { value: nome, text: nome })));
  const contasTikTok = (state.accounts && state.accounts.length)
    ? state.accounts
    : [{ id: 'default', name: 'Conta Principal' }];
  const contasYouTube = (state.ytAccounts && state.ytAccounts.length)
    ? state.ytAccounts
    : [{ id: 'default', name: 'Canal Principal' }];
  const larguraConta = 'width:210px; flex:0 0 auto';
  const contaTikTok = h('select', { class: 'sc-select', style: larguraConta },
    ...contasTikTok.map((conta) => h('option', { value: conta.id, text: conta.name || conta.id })));
  const contaYouTube = h('select', { class: 'sc-select', style: larguraConta },
    ...contasYouTube.map((canal) => h('option', { value: canal.id, text: canal.name || canal.id })));

  const caixaTikTok = h('input', { type: 'checkbox', checked: true });
  const caixaYouTube = h('input', { type: 'checkbox', checked: true });

  const botaoGerar = h('button', { class: 'sc-btn sc-btn-primary' }, 'Gerar história');

  const painel = h('div', { class: 'sc-card', style: 'margin-top:18px; display:none' });
  const areaVideo = h('div', { style: 'margin-top:18px; display:none' });
  const areaPublicar = h('div', { class: 'sc-card', style: 'margin-top:18px; display:none' });
  const areaAntigas = h('div', { style: 'margin-top:26px' });

  botaoGerar.addEventListener('click', async () => {
    const texto = tema.value.trim();
    if (!texto) {
      toast('Escreva o tema da história');
      return;
    }
    const plataformas = [];
    if (caixaTikTok.checked) plataformas.push('tiktok');
    if (caixaYouTube.checked) plataformas.push('youtube');

    botaoGerar.disabled = true;
    estadoDaHistoria(painel, areaVideo, areaPublicar,
      { status: 'building', percent: 1, message: 'Começando', step: 'roteiro' });
    try {
      const inicial = await apiHistoria('/fiction/build', {
        method: 'POST',
        body: JSON.stringify({
          theme: texto,
          palette: paleta.value,
          platforms: plataformas.length ? plataformas : ['tiktok'],
        }),
      });
      historiaState.projectId = inicial.project_id;
      historiaState.platforms = {
        tiktok: caixaTikTok.checked,
        youtube: caixaYouTube.checked,
      };
      acompanharHistoria(painel, areaVideo, areaPublicar, contaTikTok, contaYouTube);
    } catch (e) {
      toast('Não consegui gerar: ' + e.message);
      botaoGerar.disabled = false;
    }
  });

  wrap.append(
    h('div', { class: 'sc-card' },
      h('div', { class: 'sc-card-title', text: 'O que a história vai contar' }),
      h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Tema' }), tema),
      h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Paleta' }), paleta),
      h('div', { class: 'sc-field' },
        h('label', { class: 'sc-label', text: 'Onde publicar depois' }),
        h('div', { style: 'display:flex; gap:14px; align-items:center; flex-wrap:wrap' },
          h('label', { style: 'display:flex; gap:6px; align-items:center; white-space:nowrap' },
            caixaTikTok, h('span', { text: 'TikTok' })),
          contaTikTok,
          h('label', { style: 'display:flex; gap:6px; align-items:center; white-space:nowrap' },
            caixaYouTube, h('span', { text: 'YouTube Shorts' })),
          contaYouTube)),
      h('p', { class: 'sc-card-sub', text: 'As vozes são sintéticas do sistema (narrador, personagem e sussurro). Não há clonagem de voz de pessoa real.' }),
      botaoGerar),
    painel,
    areaVideo,
    areaPublicar,
    areaAntigas,
  );

  // Uma história já aberta nesta sessão volta a ser acompanhada.
  if (historiaState.projectId) {
    acompanharHistoria(painel, areaVideo, areaPublicar, contaTikTok, contaYouTube);
  }
  carregarHistoriasAntigas(areaAntigas, painel, areaVideo, areaPublicar,
    contaTikTok, contaYouTube);

  container.replaceChildren(wrap);
}

function estadoDaHistoria(painel, areaVideo, areaPublicar, estado) {
  painel.style.display = 'block';
  painel.replaceChildren();
  const percentual = Math.max(0, Math.min(100, Number(estado.percent) || 0));
  const barra = h('div', { class: 'sc-progress-fill', style: `width:${percentual}%` });
  painel.append(
    h('div', { class: 'sc-card-title', text: historiaRotulo(estado.status) === 'pronto'
      ? 'História pronta' : 'Gerando a história' }),
    h('div', { class: 'sc-progress-track' }, barra),
    h('p', { class: 'sc-progress-hint', text: `${percentual}% — ${estado.message || estado.step || ''}` }),
  );
  if (estado.status === 'failed' && estado.error) {
    painel.append(h('p', { style: 'color:var(--danger,#e5534b); margin-top:8px', text: estado.error }));
  }
  if (estado.status !== 'ready') {
    areaVideo.style.display = 'none';
    areaPublicar.style.display = 'none';
  }
}

function acompanharHistoria(painel, areaVideo, areaPublicar, contaTikTok, contaYouTube) {
  limparTimersHistoria();
  const projectId = historiaState.projectId;
  if (!projectId) return;

  const consultar = async () => {
    let estado;
    try {
      estado = await apiHistoria(`/fiction/state/${projectId}`);
    } catch (e) {
      toast('Perdi o estado da história: ' + e.message);
      limparTimersHistoria();
      return;
    }
    historiaState.build = estado;
    estadoDaHistoria(painel, areaVideo, areaPublicar, estado);

    if (estado.status === 'building') return;

    limparTimersHistoria();
    if (estado.status === 'ready') {
      mostrarVideo(areaVideo, areaPublicar, estado, contaTikTok, contaYouTube);
    }
  };

  consultar();
  historiaState.timer = setInterval(consultar, 2500);
}

function mostrarVideo(areaVideo, areaPublicar, estado, contaTikTok, contaYouTube) {
  const url = `${HISTORIA_API}/fiction/video/${estado.project_id}?t=${Date.now()}`;
  areaVideo.style.display = 'block';
  areaVideo.replaceChildren(h('div', { class: 'sc-card' },
    h('div', { class: 'sc-card-title', text: estado.title || 'História' }),
    h('p', { class: 'sc-card-sub', text: `${historiaSegundos(estado.duration)} · ${estado.scenes || 6} cenas · ${estado.cues || 0} legendas · ${estado.word_count || '?'} palavras · tempos ${(estado.timing || []).join('/')}` }),
    h('video', { src: url, controls: true, playsinline: true, style: 'width:100%; max-width:360px; border-radius:14px; background:#000' }),
    h('div', { style: 'display:flex; gap:10px; margin-top:12px; flex-wrap:wrap' },
      h('a', { class: 'sc-btn sc-btn-sm sc-btn-ghost', href: `${HISTORIA_API}/fiction/player/${estado.project_id}?preview`, target: '_blank', rel: 'noopener' }, 'Ver no navegador'),
      h('button', { class: 'sc-btn sc-btn-sm sc-btn-ghost', onclick: () => navegar('biblioteca') }, 'Abrir na Biblioteca'),
      h('a', { class: 'sc-btn sc-btn-sm sc-btn-ghost', href: url, download: `${estado.project_id}.mp4` }, 'Baixar MP4')),
  ));

  const log = h('div', { style: 'font-family:monospace; font-size:12px; white-space:pre-wrap; max-height:180px; overflow:auto; margin-top:10px; color:var(--text-muted)' });
  const botaoPublicar = h('button', { class: 'sc-btn sc-btn-primary' }, 'Publicar agora');

  botaoPublicar.addEventListener('click', async () => {
    const plataformas = [];
    if (historiaState.platforms.tiktok) plataformas.push('tiktok');
    if (historiaState.platforms.youtube) plataformas.push('youtube');
    botaoPublicar.disabled = true;
    try {
      await apiHistoria('/fiction/publish', {
        method: 'POST',
        body: JSON.stringify({
          project_id: estado.project_id,
          platforms: plataformas,
          tiktok_account: contaTikTok.value,
          youtube_account: contaYouTube.value,
          interval_sec: 30,
        }),
      });
      toast('Publicação iniciada');
      acompanharPublicacao(log, botaoPublicar);
    } catch (e) {
      toast('Não consegui publicar: ' + e.message);
      botaoPublicar.disabled = false;
    }
  });

  areaPublicar.style.display = 'block';
  areaPublicar.replaceChildren(
    h('div', { class: 'sc-card-title', text: 'Publicar' }),
    h('p', { class: 'sc-card-sub', text: 'Usa os publicadores locais do TikClip (navegador já logado). Nada é enviado duas vezes para a mesma plataforma.' }),
    h('div', { style: 'display:flex; gap:14px; align-items:center; flex-wrap:wrap' },
      h('label', { style: 'display:flex; gap:6px; align-items:center; white-space:nowrap' },
        h('input', {
          type: 'checkbox',
          checked: historiaState.platforms.tiktok,
          onchange: (e) => { historiaState.platforms.tiktok = e.target.checked; },
        }), h('span', { text: 'TikTok' })),
      contaTikTok,
      h('label', { style: 'display:flex; gap:6px; align-items:center; white-space:nowrap' },
        h('input', {
          type: 'checkbox',
          checked: historiaState.platforms.youtube,
          onchange: (e) => { historiaState.platforms.youtube = e.target.checked; },
        }), h('span', { text: 'YouTube Shorts' })),
      contaYouTube),
    botaoPublicar,
    log,
  );
}

function acompanharPublicacao(log, botaoPublicar) {
  if (historiaState.pubTimer) clearInterval(historiaState.pubTimer);
  const consultar = async () => {
    let estado;
    try {
      estado = await apiHistoria('/fiction/publish/state');
    } catch (e) {
      log.textContent = 'Falha ao ler o estado da publicação: ' + e.message;
      return;
    }
    log.textContent = (estado.log || []).slice(-12).join('\n');
    if (estado.env_locked) {
      log.textContent = 'Publicação travada por ambiente (TIKCLIP_DISABLE_AUTO_PUBLISH).';
    }
    if (!estado.running) {
      clearInterval(historiaState.pubTimer);
      historiaState.pubTimer = null;
      botaoPublicar.disabled = false;
      const publicados = (estado.published || []).length;
      const falhas = (estado.failed || []).length;
      toast(`Publicação concluída: ${publicados} enviada(s), ${falhas} falha(s)`);
    }
  };
  consultar();
  historiaState.pubTimer = setInterval(consultar, 2000);
}

async function carregarHistoriasAntigas(area, painel, areaVideo, areaPublicar,
  contaTikTok, contaYouTube) {
  let itens = [];
  try {
    const dados = await apiHistoria('/fiction/projects');
    itens = dados.items || [];
  } catch (e) {
    area.replaceChildren(h('p', { class: 'sc-card-sub', text: 'Não consegui listar as histórias: ' + e.message }));
    return;
  }
  if (!itens.length) {
    area.replaceChildren(h('p', { class: 'sc-card-sub', text: 'Nenhuma história gerada ainda.' }));
    return;
  }
  const lista = h('div', { class: 'sc-card' });
  lista.append(h('div', { class: 'sc-card-title', text: 'Histórias deste computador' }));
  for (const item of itens.slice(0, 20)) {
    lista.append(h('div', {
      style: 'display:flex; gap:10px; align-items:center; justify-content:space-between; padding:8px 0; border-bottom:1px solid var(--border,#26262b)',
    },
    h('div', {},
      h('div', { style: 'font-weight:600', text: item.title || 'Sem título' }),
      h('div', { class: 'sc-card-sub', text: `${historiaSegundos(item.duration)} · ${historiaRotulo(item.status)}` })),
    h('button', {
      class: 'sc-btn sc-btn-sm sc-btn-ghost',
      onclick: () => {
        historiaState.projectId = item.project_id;
        acompanharHistoria(painel, areaVideo, areaPublicar, contaTikTok, contaYouTube);
        toast('Abrindo ' + (item.title || item.project_id));
      },
    }, 'Abrir')));
  }
  area.replaceChildren(lista);
}
