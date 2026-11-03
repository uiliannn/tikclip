/* ==========================================================================
   TikClip Web — Cliente SupoClip Edition
   100% Português · Sem texto em chinês · Suporte Dual Gemini & DeepSeek
   ========================================================================== */
'use strict';

const API_EXT = '/api/v1/ext';
const API_SUPO = '/api/v1/supo';
const API_MOTOR = '/api/v1';

const state = {
  view: 'criar',
  estudioTab: 'gerar',
  accounts: [],
  activeAccount: 'default',
  ytAccounts: [],
  activeYtAccount: 'default',
  ytPubTab: 'shorts',
  projects: [],
  currentProjectId: null,
  currentClips: [],
  selectedClips: new Set(),
  ai: {
    active_provider: 'deepseek',
    active_model: 'deepseek-chat',
    analysis_mode: 'auto',
    whisper_model: 'large-v3-turbo',
    max_clips: 5,
    gemini: { has_key: false, masked_key: '', model: 'gemini-3.5-flash', models: [] },
    deepseek: { has_key: false, masked_key: '', model: 'deepseek-chat', base_url: 'https://api.deepseek.com', models: [] },
  },
  supoSettings: {
    language: 'pt-BR',
    subtitle: {
      font_family: 'Tiktoksans Regular',
      font_size: 24,
      text_color: '#FFFFFF',
    },
    defaults: {
      duration_mode: 'medium',
      duration_seconds: 45,
      aspect: 'portrait',
      goal: 'auto',
      clean_fillers: true,
      auto_start: true,
    },
    notifications: {
      sound_on_complete: true,
    },
  },
  progressTimer: null,
  publishTimer: null,
  estudioTimer: null,
  ytPubTimer: null,
  publishInterval: 30,
  tiktokCreators: [],
  selectedTikTokCreator: null,
  selectedTikTokVideos: new Set(),
  tiktokVideosCache: {},
};

/* ---------------------------------------------------------- Utilitários --- */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

let toastTimeout = null;
function toast(msg) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('is-visible');
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => el.classList.remove('is-visible'), 3200);
}

async function req(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || data.mensagem || data.message || detail;
      if (Array.isArray(detail)) {
        detail = detail.map((d) => d.msg || (d.loc ? `${d.loc.join('.')}: ${d.msg}` : JSON.stringify(d))).join('; ');
      } else if (typeof detail === 'object' && detail !== null) {
        detail = detail.msg || detail.message || JSON.stringify(detail);
      }
    } catch { /* ignore */ }
    throw new Error(String(detail));
  }
  if (res.status === 204) return null;
  return res.json();
}

function apiExt(path, opts = {}) {
  return req(API_EXT + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
}

function apiSupo(path, opts = {}) {
  return req(API_SUPO + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
}

function saudacaoHorario() {
  const hora = new Date().getHours();
  if (hora >= 5 && hora < 12) return 'Bom dia, Criador';
  if (hora >= 12 && hora < 18) return 'Boa tarde, Criador';
  return 'Boa noite, Criador';
}

function tempoRelativo(epochSec) {
  if (!epochSec) return 'Recente';
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - epochSec));
  if (diff < 60) return 'Agora mesmo';
  if (diff < 3600) return `Há ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `Há ${Math.floor(diff / 3600)} h`;
  return `Há ${Math.floor(diff / 86400)} d`;
}

function limparTimers() {
  if (state.progressTimer) { clearInterval(state.progressTimer); state.progressTimer = null; }
  if (state.publishTimer) { clearInterval(state.publishTimer); state.publishTimer = null; }
  if (state.estudioTimer) { clearInterval(state.estudioTimer); state.estudioTimer = null; }
  if (state.ytPubTimer) { clearInterval(state.ytPubTimer); state.ytPubTimer = null; }
}

/* --------------------------------------------------------- Tema & Barra --- */
function aplicarTema(tema) {
  document.documentElement.setAttribute('data-theme', tema);
  localStorage.setItem('sc-theme', tema);
  const label = document.getElementById('theme-label');
  if (label) label.textContent = tema === 'dark' ? 'Modo claro' : 'Modo escuro';
}

function atualizarSidebarStatus() {
  const aiLabel = document.getElementById('sidebar-ai-label');
  if (aiLabel) {
    const isGem = state.ai && state.ai.active_provider === 'gemini';
    const gemModel = (state.ai && state.ai.gemini && state.ai.gemini.model) || '2.5-flash';
    const dsModel = (state.ai && state.ai.deepseek && state.ai.deepseek.model) || 'chat';
    aiLabel.textContent = isGem
      ? `IA: Google Gemini (${gemModel})`
      : `IA: DeepSeek (${dsModel})`;
  }
  const ttLabel = document.getElementById('sidebar-tt-label');
  const accCount = document.getElementById('nav-acc-count');
  if (accCount) accCount.textContent = String(state.accounts.length || 0);
  if (ttLabel) {
    const conta = state.accounts.find((a) => a.id === state.activeAccount) || state.accounts[0];
    ttLabel.textContent = conta ? `TikTok: ${conta.name}` : 'TikTok: Sem conta';
  }
  const chanCount = document.getElementById('nav-channels-count');
  if (chanCount) {
    const ativ = (state.channels || []).filter((c) => c.active).length;
    chanCount.textContent = String(ativ);
    chanCount.style.display = ativ > 0 ? 'inline-flex' : 'none';
  }
  const ytCount = document.getElementById('nav-yt-acc-count');
  if (ytCount) {
    const totalYt = (state.ytAccounts || []).length;
    ytCount.textContent = String(totalYt);
    ytCount.style.display = totalYt > 0 ? 'inline-flex' : 'none';
  }
  const tiktokCount = document.getElementById('nav-tiktok-count');
  if (tiktokCount) {
    const totalVids = (state.tiktokCreators || []).reduce((acc, c) => acc + (c.video_count || 0), 0);
    tiktokCount.textContent = String(totalVids);
    tiktokCount.style.display = totalVids > 0 ? 'inline-flex' : 'none';
  }
}

async function carregarEstadoInicial() {
  try {
    const [st, proj, aiCfg, supoCfg, chanCfg, ttData, ytData] = await Promise.all([
      apiExt('/status').catch(() => ({ accounts: [] })),
      apiExt('/projects').catch(() => ({ items: [] })),
      apiSupo('/ai').catch(() => null),
      apiSupo('/settings').catch(() => null),
      apiSupo('/channels').catch(() => ({ channels: [], project_meta: {} })),
      apiSupo('/tiktok-downloads/creators').catch(() => ({ creators: [] })),
      apiSupo('/youtube-publisher/accounts').catch(() => ({ accounts: [] })),
    ]);
    state.accounts = st.accounts || [];
    if (state.accounts.length && !state.accounts.some((a) => a.id === state.activeAccount)) {
      state.activeAccount = state.accounts[0].id;
    }
    state.projects = proj.items || [];
    if (aiCfg && aiCfg.ok) state.ai = aiCfg;
    if (supoCfg && supoCfg.settings) state.supoSettings = supoCfg.settings;
    if (chanCfg) {
      state.channels = chanCfg.channels || [];
      state.projectChannels = chanCfg.project_meta || {};
    }
    if (ttData && ttData.creators) {
      state.tiktokCreators = ttData.creators || [];
    }
    if (ytData && ytData.accounts) {
      state.ytAccounts = ytData.accounts || [];
    }
    atualizarSidebarStatus();
  } catch (e) {
    console.warn('Erro ao carregar estado inicial:', e);
  }
}

function navegar(novaView, pid = null) {
  limparTimers();
  state.view = novaView;
  if (pid) state.currentProjectId = pid;

  document.querySelectorAll('#sidebar-nav .sc-nav-item').forEach((btn) => {
    const ativo = btn.dataset.view === novaView || (novaView === 'projeto' && btn.dataset.view === 'biblioteca');
    btn.classList.toggle('is-active', ativo);
  });

  const main = document.getElementById('main-view');
  main.replaceChildren();

  if (novaView === 'criar') renderCriar(main);
  else if (novaView === 'biblioteca') renderBiblioteca(main);
  else if (novaView === 'projeto') renderProjeto(main, state.currentProjectId);
  else if (novaView === 'publicador') renderPublicador(main);
  else if (novaView === 'youtube_publisher') renderYouTubePublisher(main);
  else if (novaView === 'canais') renderCanais(main);
  else if (novaView === 'tiktok_downloads') renderTikTokDownloads(main);
  else if (novaView === 'estudio') renderEstudio(main);
  else if (novaView === 'agentes') renderAgentes(main);
  else if (novaView === 'configuracoes') renderConfiguracoes(main);
}

/* ==========================================================================
   VIEW 1: CRIAR (HOME SUPOCLIP - PRINT 2)
   ========================================================================== */
function renderCriar(container) {
  const wrap = h('div', { class: 'sc-create-wrap' });

  // Hero Greeting
  wrap.append(
    h('div', { class: 'sc-hero' },
      h('h1', { class: 'sc-hero-title', text: saudacaoHorario() }),
      h('p', { class: 'sc-hero-sub', text: 'Solte um vídeo longo. Receba clipes legendados, prontos para publicar.' }),
    ),
  );

  // Banner de Status do YouTube (se houver aviso ou botão rápido de cookies)
  const alertBanner = h('div', { class: 'sc-alert-banner', style: 'display:none' });
  wrap.append(alertBanner);

  apiExt('/youtube/status').then((yt) => {
    const bloq = yt.bloqueio || {};
    if (bloq.bloqueado) {
      alertBanner.style.display = 'block';
      alertBanner.replaceChildren(
        h('div', { class: 'sc-alert-head' },
          h('span', null, '⚠️ Importações do YouTube - Atenção ao Limite de IP'),
          h('button', { class: 'sc-alert-link', onclick: () => navegar('configuracoes') }, 'Configurar Cookies →'),
        ),
        h('p', null, `O YouTube recusou uma conexão há ${bloq.ha_minutos || 1} min. Você também pode soltar um arquivo de vídeo local abaixo ou importar da sua pasta de Downloads.`),
      );
    }
  }).catch(() => {});

  // Estado local do composer
  let arquivoSelecionado = null;
  let duracaoVal = '45';
  let goalVal = 'auto';
  let aspectVal = 'portrait';
  let autoStart = true;

  const fileInput = h('input', {
    type: 'file',
    accept: 'video/*',
    style: 'display:none',
    onchange: (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) selecionarArquivo(f);
    },
  });

  const fileBadge = h('div', { style: 'display:none' });

  function selecionarArquivo(file) {
    arquivoSelecionado = file;
    fileBadge.style.display = 'block';
    fileBadge.replaceChildren(
      h('div', { class: 'sc-file-pill' },
        h('span', { text: `🎬 ${file.name} (${(file.size / 1048576).toFixed(1)} MB)` }),
        h('button', {
          class: 'sc-btn-ghost sc-btn-sm',
          text: '✕',
          onclick: () => {
            arquivoSelecionado = null;
            fileInput.value = '';
            fileBadge.style.display = 'none';
          },
        }),
      ),
    );
  }

  const urlInput = h('input', {
    class: 'sc-composer-input',
    type: 'text',
    placeholder: 'Cole um link do YouTube ou solte um arquivo de vídeo',
    onkeydown: (e) => {
      if (e.key === 'Enter') dispararGeracao();
    },
  });

  // Seletor rápido de duração (estilo SupoClip)
  const selectDuracao = h('select', {
    class: 'sc-chip-select',
    title: 'Duração dos cortes',
    onchange: (e) => { duracaoVal = e.target.value; },
  },
    h('option', { value: '', text: '⏱️ IA decide' }),
    h('option', { value: '25', text: '⏱️ Curto (15–30 s)' }),
    h('option', { value: '45', selected: true, text: '⏱️ Médio (30–60 s)' }),
    h('option', { value: '75', text: '⏱️ Longo (60–90 s)' }),
  );

  // Seletor de Objetivo (Valores esperados pelo backend: 'auto', 'highlight', 'content', 'promo')
  const selectGoal = h('select', {
    class: 'sc-chip-select',
    title: 'Estilo de corte',
    onchange: (e) => { goalVal = e.target.value; },
  },
    h('option', { value: 'auto', selected: true, text: '🎯 Default' }),
    h('option', { value: 'highlight', text: '🔥 Momentos Virais' }),
    h('option', { value: 'content', text: '🎙️ Cortes de Podcast' }),
    h('option', { value: 'promo', text: '⚡ Dinâmico / Ação' }),
  );

  // Seletor de Proporção
  const selectAspect = h('select', {
    class: 'sc-chip-select',
    title: 'Proporção do vídeo',
    onchange: (e) => { aspectVal = e.target.value; },
  },
    h('option', { value: 'portrait', selected: true, text: '📐 Auto 9:16' }),
    h('option', { value: 'landscape', text: '📐 Horizontal 16:9' }),
    h('option', { value: 'square', text: '📐 Quadrado 1:1' }),
  );

  // Chip rápido para alternar IA (Gemini vs DeepSeek) direto na Home!
  const isGem = state.ai.active_provider === 'gemini';
  const btnAiQuick = h('button', {
    type: 'button',
    class: `sc-chip sc-chip-ai ${isGem ? '' : 'is-deepseek'}`,
    title: 'Clique para alternar entre Google Gemini e DeepSeek',
    onclick: async () => {
      const novo = state.ai.active_provider === 'gemini' ? 'deepseek' : 'gemini';
      try {
        const res = await apiSupo('/ai/switch', {
          method: 'POST',
          body: JSON.stringify({ provider: novo }),
        });
        state.ai = res;
        atualizarSidebarStatus();
        const g = state.ai.active_provider === 'gemini';
        btnAiQuick.className = `sc-chip sc-chip-ai ${g ? '' : 'is-deepseek'}`;
        btnAiQuick.textContent = g ? '✨ IA: Gemini' : '🧠 IA: DeepSeek';
        toast(`IA alterada para ${g ? 'Google Gemini' : 'DeepSeek'}!`);
      } catch (err) {
        toast('Erro ao alternar IA: ' + err.message);
      }
    },
  }, isGem ? '✨ IA: Gemini' : '🧠 IA: DeepSeek');

  const btnLimpeza = h('button', {
    type: 'button',
    class: 'sc-chip',
    title: 'Limpeza automática: Sem vinheta de marca + Auto-iniciar cortes',
    onclick: () => {
      autoStart = !autoStart;
      badgeLimpeza.textContent = autoStart ? '2' : '1';
      toast(autoStart ? 'Limpeza 2 ativa: Sem vinheta + Início automático' : 'Início manual selecionado');
    },
  });
  const badgeLimpeza = h('span', { class: 'sc-chip-badge', text: '2' });
  btnLimpeza.append('🪄 Limpeza ', badgeLimpeza);

  const btnSubmit = h('button', {
    type: 'button',
    class: 'sc-submit-circle',
    title: 'Iniciar geração de clipes',
    onclick: dispararGeracao,
  }, '↑');

  const composer = h('div', {
    class: 'sc-composer',
    ondragover: (e) => { e.preventDefault(); composer.classList.add('is-dragover'); },
    ondragleave: () => composer.classList.remove('is-dragover'),
    ondrop: (e) => {
      e.preventDefault();
      composer.classList.remove('is-dragover');
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) selecionarArquivo(f);
    },
  },
    h('div', { class: 'sc-composer-top' },
      h('div', { class: 'sc-link-icon' }, '🔗'),
      urlInput,
    ),
    fileBadge,
    fileInput,
    h('div', { class: 'sc-composer-bar' },
      h('button', { type: 'button', class: 'sc-chip', onclick: () => fileInput.click() }, '📎 Enviar'),
      selectDuracao,
      selectGoal,
      selectAspect,
      btnLimpeza,
      btnAiQuick,
      btnSubmit,
    ),
  );

  wrap.append(composer);

  wrap.append(
    h('p', { class: 'sc-composer-footnote' },
      'Envios de até 4 GB. Vídeos acima de 1080p ou 60 fps são otimizados automaticamente, mantendo a proporção. ',
      'Processamento local com Whisper e IA configurada em ',
      h('b', { onclick: () => navegar('configuracoes') }, 'Configurações'),
      '.',
    ),
  );

  async function dispararGeracao() {
    const url = urlInput.value.trim();
    if (!arquivoSelecionado && !url) {
      toast('Cole um link do YouTube ou selecione um arquivo de vídeo.');
      urlInput.focus();
      return;
    }
    if (!arquivoSelecionado && !/^https?:\/\//i.test(url)) {
      toast('O link precisa começar com https://');
      return;
    }

    btnSubmit.disabled = true;
    toast('Preparando importação e iniciando geração...');

    try {
      let nomeProj = `Projeto ${new Date().toLocaleDateString('pt-BR')} ${new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })}`;
      if (!arquivoSelecionado) {
        const check = await apiExt(`/youtube/check?url=${encodeURIComponent(url)}`).catch(() => ({ ok: true }));
        if (check && check.ok === false) {
          toast(check.mensagem || 'Link do YouTube inválido.');
          btnSubmit.disabled = false;
          return;
        }
        if (check && check.titulo) nomeProj = check.titulo.slice(0, 180);
      } else {
        nomeProj = arquivoSelecionado.name.replace(/\.[^.]+$/, '');
      }

      const fd = new FormData();
      fd.append('goal', goalVal);
      fd.append('language', 'source');
      fd.append('aspect', aspectVal);
      if (duracaoVal) fd.append('duration', duracaoVal);
      fd.append('platforms', 'tiktok');
      fd.append('auto_start', String(autoStart));
      fd.append('brand_outro_enabled', 'false');
      fd.append('name', nomeProj);
      if (arquivoSelecionado) fd.append('video', arquivoSelecionado, arquivoSelecionado.name);
      else fd.append('url', url);

      const res = await req(API_MOTOR + '/studio/import', { method: 'POST', body: fd });
      const pid = res.project_id;
      toast('Vídeo recebido! Iniciando pipeline de cortes...');
      await carregarEstadoInicial();
      navegar('projeto', pid);
    } catch (err) {
      toast('Erro ao iniciar: ' + err.message);
      btnSubmit.disabled = false;
    }
  }

  container.append(wrap);

  // Seção Recentes (Print 2 do SupoClip)
  const recentSec = h('section', { class: 'sc-recent-section' },
    h('div', { class: 'sc-section-head' },
      h('h2', { class: 'sc-section-title', text: 'Recentes' }),
      h('button', { class: 'sc-link-subtle', onclick: () => navegar('biblioteca') }, 'Ver biblioteca →'),
    ),
  );

  const recentGrid = h('div', { class: 'sc-recent-grid' });
  const recentes = state.projects.slice(0, 4);

  if (!recentes.length) {
    recentGrid.append(
      h('div', { class: 'sc-card', style: 'grid-column: 1 / -1; text-align:center; color: var(--text-muted)' },
        'Nenhum projeto recente ainda. Cole um link ou envie um vídeo acima para começar!',
      ),
    );
  } else {
    for (const p of recentes) {
      const card = h('div', {
        class: 'sc-recent-card',
        onclick: () => navegar('projeto', p.id),
      });
      if (p.has_thumb) {
        card.append(h('img', { class: 'sc-recent-thumb', src: p.thumb_url, alt: p.title || p.name }));
      }
      const statusTxt = p.clips_ready > 0
        ? `${p.clips_ready} clipe(s) pronto(s)`
        : 'Encontrando os melhores momentos...';
      card.append(
        h('div', { class: 'sc-recent-overlay' },
          h('div', { class: 'sc-recent-title', text: p.title || p.name }),
          h('div', { class: 'sc-recent-meta', text: `${statusTxt} · ${tempoRelativo(p.updated_at)}` }),
        ),
      );
      recentGrid.append(card);
    }
  }
  recentSec.append(recentGrid);

  // Atalho para Vídeos já baixados no computador (Downloads / YT Saver)
  const localSec = h('div', { style: 'margin-top: 32px' });
  apiExt('/downloads?limite=6').then((dl) => {
    const itens = (dl && dl.itens) || [];
    if (!itens.length) return;
    localSec.append(
      h('div', { class: 'sc-section-head' },
        h('h3', { class: 'sc-section-title', style: 'font-size:14.5px', text: '📂 Vídeos encontrados na sua pasta Downloads / YT Saver (Importação em 1 clique)' }),
      ),
    );
    const list = h('div', { style: 'display:grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px;' });
    for (const item of itens) {
      list.append(
        h('div', { class: 'sc-card', style: 'margin:0; padding:12px 14px; display:flex; align-items:center; justify-content:space-between; gap:10px;' },
          h('div', { style: 'overflow:hidden' },
            h('div', { style: 'font-weight:600; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;', text: item.nome }),
            h('div', { style: 'font-size:11.5px; color:var(--text-muted)', text: `${item.tamanho_mb} MB · ${item.pasta}` }),
          ),
          item.project_id
            ? h('button', { class: 'sc-btn sc-btn-sm', onclick: () => navegar('projeto', item.project_id) }, 'Abrir')
            : h('button', {
                class: 'sc-btn sc-btn-primary sc-btn-sm',
                onclick: async (ev) => {
                  ev.target.disabled = true;
                  ev.target.textContent = 'Importando...';
                  try {
                    const r = await apiExt('/downloads/import', {
                      method: 'POST',
                      body: JSON.stringify({ id: item.id, duration: Number(duracaoVal) || null }),
                    });
                    await carregarEstadoInicial();
                    navegar('projeto', r.project_id);
                  } catch (e) {
                    toast('Erro: ' + e.message);
                    ev.target.disabled = false;
                    ev.target.textContent = 'Cortar';
                  }
                },
              }, 'Cortar'),
        ),
      );
    }
    localSec.append(list);
  }).catch(() => {});

  recentSec.append(localSec);
  container.append(recentSec);
}

/* ==========================================================================
   VIEW 2: DETALHE DO PROJETO + PROGRESSO EM 4 ETAPAS (PRINT 1 DO SUPOCLIP)
   ========================================================================== */
async function renderProjeto(container, pid) {
  if (!pid) {
    navegar('biblioteca');
    return;
  }

  const projMeta = state.projects.find((p) => p.id === pid) || { id: pid, title: `Projeto ${pid.slice(0, 8)}` };

  // Topo estilo Print 1 do SupoClip
  const backBtn = h('button', {
    class: 'sc-back-link',
    onclick: () => navegar('biblioteca'),
  }, '← Biblioteca');

  const headerTitle = h('h1', { class: 'sc-proj-title', text: projMeta.title || projMeta.name });

  const headerSub = h('div', { class: 'sc-proj-sub' },
    h('span', null, '▶ YouTube / Local ↗'),
    h('span', null, `🕒 ${tempoRelativo(projMeta.updated_at)}`),
    h('span', null, `🤖 IA ativa: ${state.ai.active_provider === 'gemini' ? 'Google Gemini' : 'DeepSeek'}`),
  );

  const btnMaisCortes = h('button', {
    class: 'sc-btn',
    onclick: async () => {
      try {
        toast('Solicitando mais cortes do vídeo...');
        await apiExt(`/projects/${pid}/mais-cortes`, { method: 'POST', body: JSON.stringify({ quantidade: 3 }) });
        atualizarTelaProjeto();
      } catch (e) {
        toast('Aviso: ' + e.message);
      }
    },
  }, '+ Mais cortes');

  const btnRenderPendentes = h('button', {
    class: 'sc-btn',
    onclick: async () => {
      try {
        await apiExt('/render/pending', { method: 'POST', body: JSON.stringify({ project_id: pid }) });
        toast('Renderização de cortes pendentes iniciada!');
        atualizarTelaProjeto();
      } catch (e) {
        toast('Erro: ' + e.message);
      }
    },
  }, 'Renderizar pendentes');

  const btnExcluir = h('button', {
    class: 'sc-btn',
    style: 'color: var(--err-color)',
    onclick: async () => {
      if (!confirm('Deseja excluir este projeto da biblioteca?')) return;
      try {
        await req(`${API_MOTOR}/projects/${pid}`, { method: 'DELETE' });
        toast('Projeto removido.');
        await carregarEstadoInicial();
        navegar('biblioteca');
      } catch (e) {
        toast('Erro ao excluir: ' + e.message);
      }
    },
  }, 'Excluir');

  const header = h('div', { class: 'sc-proj-header' },
    h('div', null, headerTitle, headerSub),
    h('div', { class: 'sc-proj-actions' }, btnMaisCortes, btnRenderPendentes, btnExcluir),
  );

  // Painel de Progresso em 4 Etapas (Idêntico ao SupoClip)
  const progressPanel = h('div', { class: 'sc-progress-panel', style: 'display:none' });
  // Painel de Hashtags Globais do Projeto
  const hashtagsPanel = h('div', { class: 'sc-hashtags-panel' });
  const clipsSection = h('div');

  container.append(backBtn, header, progressPanel, hashtagsPanel, clipsSection);

  let projectTags = [];

  function renderHashtags() {
    hashtagsPanel.replaceChildren();

    const titleRow = h('div', { class: 'sc-hashtags-header' },
      h('div', { class: 'sc-hashtags-title' },
        '🏷️ Hashtags do Projeto',
        h('span', { class: 'sc-badge-count', text: `${projectTags.length} tag(s)` }),
      ),
      h('div', { style: 'display:flex; align-items:center; gap:8px;' },
        h('button', {
          class: 'sc-btn',
          style: 'font-size:12px; padding:4px 10px;',
          title: 'Usa a IA configurada para sugerir hashtags virais com base no conteúdo',
          onclick: async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            btn.textContent = '✨ Gerando...';
            try {
              const res = await req('/api/v1/supo/projects/' + pid + '/hashtags/ai-suggest', { method: 'POST' });
              if (res.tags) {
                projectTags = res.tags;
                renderHashtags();
                toast(`Sugeridas ${res.adicionadas ? res.adicionadas.length : ''} novas hashtags com IA!`);
                await atualizarTelaProjeto();
              }
            } catch (err) {
              toast('Falha ao sugerir: ' + (err.message || err));
            } finally {
              btn.disabled = false;
              btn.textContent = '✨ Sugerir com IA';
            }
          },
        }, '✨ Sugerir com IA'),
      ),
    );

    const chipsWrap = h('div', { class: 'sc-hashtags-chips' });
    if (!projectTags.length) {
      chipsWrap.append(h('span', { class: 'sc-hashtags-empty', text: 'Nenhuma hashtag configurada ainda. Adicione abaixo para incluir em todos os cortes automaticamente.' }));
    } else {
      projectTags.forEach((tag, idx) => {
        const chip = h('span', { class: 'sc-hashtag-chip' },
          `#${tag}`,
          h('button', {
            class: 'sc-hashtag-remove',
            title: 'Remover hashtag',
            onclick: async () => {
              const novas = projectTags.filter((_, i) => i !== idx);
              await salvarHashtags(novas);
            },
          }, '×'),
        );
        chipsWrap.append(chip);
      });
    }

    const inputTag = h('input', {
      class: 'sc-input sc-hashtag-input',
      type: 'text',
      placeholder: 'Adicione hashtags separadas por espaço ou vírgula (ex: #carros, #gasmonkey)...',
      onkeydown: async (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          await adicionarTagsInput(inputTag.value);
          inputTag.value = '';
        }
      },
    });

    const btnAdd = h('button', {
      class: 'sc-btn sc-btn-primary',
      style: 'font-size:12.5px; padding:6px 14px;',
      onclick: async () => {
        await adicionarTagsInput(inputTag.value);
        inputTag.value = '';
      },
    }, '+ Adicionar');

    const formRow = h('div', { class: 'sc-hashtag-form' }, inputTag, btnAdd);
    const hint = h('div', { class: 'sc-hashtags-hint' },
      '💡 Estas hashtags serão aplicadas automaticamente em todos os cortes do projeto ao publicar no TikTok.',
    );

    hashtagsPanel.append(titleRow, chipsWrap, formRow, hint);
  }

  async function adicionarTagsInput(texto) {
    if (!texto || !texto.trim()) return;
    const pedacos = texto
      .split(/[,\s]+/)
      .map((t) => t.replace(/^[#\s]+|[#\s]+$/g, '').toLowerCase().trim())
      .filter((t) => t.length >= 2);
    if (!pedacos.length) return;

    const juntas = Array.from(new Set([...projectTags, ...pedacos])).slice(0, 20);
    await salvarHashtags(juntas);
  }

  async function salvarHashtags(tags) {
    try {
      await apiExt(`/projects/${pid}/hashtags`, {
        method: 'PUT',
        body: JSON.stringify({ tags }),
      });
      projectTags = tags;
      renderHashtags();
      toast('Hashtags do projeto salvas com sucesso!');
      await atualizarTelaProjeto();
    } catch (err) {
      toast('Erro ao salvar hashtags: ' + (err.message || err));
    }
  }

  // Carrega as tags iniciais do projeto
  apiExt(`/projects/${pid}/hashtags`)
    .then((res) => {
      projectTags = res.tags || [];
      renderHashtags();
    })
    .catch(() => {
      renderHashtags();
    });

  function montarStepper(prog) {
    const rawPct = Number(prog.percent) || 0;
    const faseId = (prog.fase_id || '').toLowerCase();
    const fase = (prog.fase || '').toLowerCase();
    const situacao = (prog.situacao || '').toLowerCase();
    const cortesTotal = (prog.cortes && prog.cortes.total) || 0;
    const cortesProntos = (prog.cortes && prog.cortes.prontos) || 0;

    let etapaIdx = 0; // 0: buscando, 1: transcrevendo, 2: momentos, 3: renderizando
    let pct = rawPct;
    let tituloEta = '';

    const isDone = situacao === 'concluido' || (cortesTotal > 0 && cortesProntos === cortesTotal);
    const isFailed = situacao === 'falhou' || prog.analysis_status === 'failed';
    const isRendering = faseId === 'rendering' || fase.includes('render') || fase.includes('gerando') || cortesTotal > 0;
    const isMoments = faseId === 'analyzing' || faseId === 'planning' || fase.includes('analis') || fase.includes('momento') || fase.includes('planej');
    const isTranscribing = faseId === 'screening' || faseId === 'transcribing' || fase.includes('transcrev') || fase.includes('legenda');

    if (isDone) {
      etapaIdx = 4;
      pct = 100;
      tituloEta = `✓ Concluído! Todos os ${cortesTotal} clipes foram gerados com sucesso!`;
    } else if (isFailed) {
      tituloEta = `Processo interrompido: ${prog.erro || 'Verifique os detalhes'}`;
    } else if (isRendering) {
      etapaIdx = 3;
      const share = cortesTotal > 0 ? (cortesProntos / cortesTotal) * 35 : 5;
      pct = Math.min(99, Math.round(65 + share));
      const restam = Math.max(0, cortesTotal - cortesProntos);
      tituloEta = `Renderizando clipes: ${cortesProntos} de ${cortesTotal} prontos (${pct}%) — ${restam} restante(s)`;
    } else if (isMoments) {
      etapaIdx = 2;
      pct = Math.max(50, Math.min(65, rawPct || 55));
      tituloEta = 'IA analisando momentos virais e ganchos...';
    } else if (isTranscribing) {
      etapaIdx = 1;
      pct = Math.max(25, Math.min(50, rawPct || 35));
      tituloEta = 'Transcrevendo áudio com Whisper...';
    } else {
      etapaIdx = 0;
      pct = Math.max(10, Math.min(25, rawPct || 15));
      tituloEta = 'Buscando vídeo e preparando mídia...';
    }

    const etapas = [
      'Buscando vídeo',
      'Transcrevendo',
      'Encontrando momentos',
      'Renderizando clipes',
    ];

    const stepsRow = h('div', { class: 'sc-steps-row' });
    etapas.forEach((nome, idx) => {
      const done = idx < etapaIdx;
      const curr = idx === etapaIdx;
      stepsRow.append(
        h('div', { class: `sc-step-item ${done ? 'is-done' : ''} ${curr ? 'is-current' : ''}` },
          done ? '✓ ' : '',
          nome,
        ),
      );
    });

    const skelGrid = h('div', { class: 'sc-skeleton-grid' });
    for (let i = 0; i < 4; i++) {
      skelGrid.append(
        h('div', { class: 'sc-skeleton-card' },
          h('div', { class: 'sc-skeleton-box' }),
          h('div', { class: 'sc-skeleton-line' }),
          h('div', { class: 'sc-skeleton-line is-short' }),
        ),
      );
    }

    progressPanel.style.display = 'block';
    const elementosPainel = [
      h('div', { class: 'sc-eta-title', text: tituloEta }),
      h('div', { class: 'sc-progress-track' },
        h('div', { class: 'sc-progress-fill', style: `width: ${Math.max(8, pct)}%` }),
      ),
      stepsRow,
      h('p', { class: 'sc-progress-hint', text: prog.mensagem
        ? `${prog.mensagem} — Você pode sair desta página; seus clipes estarão esperando na sua biblioteca.`
        : 'Você pode sair desta página — seus clipes estarão esperando na sua biblioteca.' }),
    ];

    if (isFailed) {
      const btnTentarNovamente = h('button', {
        class: 'sc-btn sc-btn-primary',
        style: 'margin-top:14px; font-weight:700; padding:10px 20px; display:inline-flex; align-items:center; gap:8px;',
        onclick: async () => {
          btnTentarNovamente.disabled = true;
          btnTentarNovamente.textContent = '⏳ Reiniciando análise dos momentos...';
          toast('Reiniciando análise dos melhores momentos com correção automática...');
          try {
            await req(`${API_MOTOR}/studio/${pid}/analyze`, { method: 'POST' });
            toast('Análise reiniciada com sucesso! Aguarde a geração dos cortes.');
            atualizarTelaProjeto();
          } catch (e) {
            toast('Erro ao reiniciar: ' + (e.message || e));
            btnTentarNovamente.disabled = false;
            btnTentarNovamente.textContent = '🔄 Tentar Novamente';
          }
        },
      }, '🔄 Tentar Novamente (Continuar Geração)');
      elementosPainel.push(btnTentarNovamente);
    }

    if (!cortesProntos) {
      elementosPainel.push(skelGrid);
    }

    progressPanel.replaceChildren(...elementosPainel);
  }

  async function atualizarTelaProjeto() {
    try {
      const [prog, clipsRes] = await Promise.all([
        apiExt(`/projects/${pid}/progresso`).catch(() => null),
        apiExt(`/projects/${pid}/clips`).catch(() => ({ items: [] })),
      ]);

      const clips = clipsRes.items || [];
      state.currentClips = clips;

      const estaAtivo = prog && (prog.ativo || prog.situacao === 'rodando' || (!clips.length && prog.situacao !== 'concluido'));
      if (estaAtivo) {
        montarStepper(prog);
      } else {
        progressPanel.style.display = 'none';
        if (state.progressTimer) {
          clearInterval(state.progressTimer);
          state.progressTimer = null;
        }
      }

      renderListaClips(clipsSection, pid, clips);
    } catch (e) {
      console.warn('Erro ao atualizar projeto:', e);
    }
  }

  await atualizarTelaProjeto();
  // Se ainda houver renderização em andamento, atualiza a cada 3s; se já concluiu, não cria timer desnecessário
  const stProg = await apiExt(`/projects/${pid}/progresso`).catch(() => null);
  if (stProg && (stProg.ativo || stProg.situacao === 'rodando')) {
    state.progressTimer = setInterval(atualizarTelaProjeto, 3000);
  }
}

function renderListaClips(container, pid, clips) {
  if (!clips.length) {
    container.replaceChildren();
    return;
  }

  let toolbar = container.querySelector('.sc-clips-toolbar');
  let grid = container.querySelector('.sc-clips-grid');

  if (!toolbar || !grid) {
    container.replaceChildren();

    const metaCanal = state.projectChannels && state.projectChannels[pid];
    if (metaCanal && metaCanal.tiktok_account) {
      state.activeAccount = metaCanal.tiktok_account;
    }

    const selConta = h('select', {
      class: 'sc-select',
      style: 'width: auto; min-width: 180px;',
      onchange: (e) => {
        state.activeAccount = e.target.value;
        atualizarSidebarStatus();
      },
    }, ...state.accounts.map((acc) =>
      h('option', { value: acc.id, selected: acc.id === state.activeAccount, text: `📱 Conta: ${acc.name}` })
    ));

    const inputIntervalo = h('input', {
      class: 'sc-input sc-input-intervalo',
      type: 'number',
      min: '0',
      max: '720',
      value: String(state.publishInterval || 30),
      style: 'width: 78px',
      title: 'Intervalo em minutos entre cada postagem no TikTok',
      oninput: (e) => {
        state.publishInterval = Math.max(0, parseInt(e.target.value) || 0);
      },
      onchange: (e) => {
        state.publishInterval = Math.max(0, parseInt(e.target.value) || 0);
      },
    });

    const btnPublicarLote = h('button', {
      class: 'sc-btn sc-btn-primary',
      onclick: async () => {
        const ids = Array.from(state.selectedClips);
        const prontos = clips.filter((c) => c.status === 'pronto').map((c) => c.id);
        const alvos = ids.length ? ids : prontos;
        if (!alvos.length) {
          toast('Nenhum corte pronto disponível para publicar.');
          return;
        }

        const disparar = async (confirmado = false) => {
          try {
            await apiExt('/publish/batch', {
              method: 'POST',
              body: JSON.stringify({
                project_id: pid,
                variant_ids: alvos,
                account_id: state.activeAccount,
                interval_min: state.publishInterval || 0,
                confirmado: confirmado,
              }),
            });
            toast(`Publicação iniciada para ${alvos.length} clipe(s) na conta ${state.activeAccount}!`);
            navegar('publicador');
          } catch (e) {
            if (e.message && e.message.includes('Confirme para continuar')) {
              if (confirm(e.message)) {
                await disparar(true);
              }
            } else {
              toast('Erro ao publicar: ' + e.message);
            }
          }
        };

        await disparar(false);
      },
    }, '🚀 Publicar no TikTok');

    const titleWrap = h('div', { style: 'display:flex; align-items:center; gap:10px;' },
      h('strong', { class: 'sc-clips-total-label', text: `${clips.length} clipe(s) no projeto` }),
      h('button', {
        class: 'sc-btn sc-btn-sm sc-btn-sel-todos',
        onclick: () => {
          if (state.selectedClips.size === clips.length) state.selectedClips.clear();
          else clips.forEach((c) => state.selectedClips.add(c.id));
          renderListaClips(container, pid, clips);
        },
      }, state.selectedClips.size ? `Limpar seleção (${state.selectedClips.size})` : 'Selecionar todos'),
    );

    const controlsWrap = h('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' },
      metaCanal ? h('span', {
        class: 'sc-chip',
        style: 'background:rgba(34,197,94,0.12); color:#15803d; border:1px solid rgba(34,197,94,0.4); font-weight:700; font-size:12px; display:inline-flex; align-items:center; gap:4px;',
        title: `Projeto gerado a partir do canal ${metaCanal.channel_name}`,
      }, `🔒 Canal: ${metaCanal.channel_name}`) : null,
      selConta,
      h('span', { style: 'font-size:12px; color:var(--text-muted)', text: 'Intervalo (min):' }),
      inputIntervalo,
      btnPublicarLote,
    );

    toolbar = h('div', { class: 'sc-clips-toolbar' }, titleWrap, controlsWrap);
    grid = h('div', { class: 'sc-clips-grid' });
    container.append(toolbar, grid);
  } else {
    const totalLabel = toolbar.querySelector('.sc-clips-total-label');
    if (totalLabel) totalLabel.textContent = `${clips.length} clipe(s) no projeto`;

    const btnSelTodos = toolbar.querySelector('.sc-btn-sel-todos');
    if (btnSelTodos) {
      btnSelTodos.textContent = state.selectedClips.size
        ? `Limpar seleção (${state.selectedClips.size})`
        : 'Selecionar todos';
    }

    const inp = toolbar.querySelector('.sc-input-intervalo');
    if (inp && document.activeElement !== inp && state.publishInterval !== undefined) {
      inp.value = String(state.publishInterval);
    }
  }

  grid.replaceChildren();

  for (const clip of clips) {
    const selecionado = state.selectedClips.has(clip.id);
    const media = h('div', { class: 'sc-clip-media' });

    if (clip.has_video && clip.video_url) {
      media.append(
        h('video', {
          class: 'sc-clip-video',
          src: clip.video_url,
          poster: clip.cover_url || '',
          controls: true,
          preload: 'metadata',
        }),
      );
    } else if (clip.cover_url) {
      media.append(h('img', { class: 'sc-clip-video', src: clip.cover_url, alt: clip.title }));
    } else {
      media.append(
        h('div', { style: 'height:100%; display:flex; align-items:center; justify-content:center; color:#999; padding:20px; text-align:center;' },
          clip.status === 'renderizando' ? '⏳ Renderizando corte...' : 'Aguardando renderização',
        ),
      );
    }

    media.append(
      h('div', { class: 'sc-clip-badges' },
        h('span', { class: `sc-pill-status is-${clip.status}`, text: clip.status }),
        clip.duration_label ? h('span', { class: 'sc-pill-status', text: clip.duration_label }) : null,
      ),
    );

    const tagsWrap = h('div', { class: 'sc-clip-tags' });
    (clip.tags || []).slice(0, 8).forEach((t) => {
      tagsWrap.append(h('span', { class: 'sc-tag', text: `#${String(t).replace(/^#/, '')}` }));
    });

    const chk = h('input', {
      type: 'checkbox',
      checked: selecionado,
      onchange: (e) => {
        if (e.target.checked) state.selectedClips.add(clip.id);
        else state.selectedClips.delete(clip.id);
        const btnSel = toolbar.querySelector('.sc-btn-sel-todos');
        if (btnSel) {
          btnSel.textContent = state.selectedClips.size
            ? `Limpar seleção (${state.selectedClips.size})`
            : 'Selecionar todos';
        }
      },
    });

    const card = h('div', { class: 'sc-clip-card' },
      media,
      h('div', { class: 'sc-clip-body' },
        h('div', { style: 'display:flex; align-items:flex-start; gap:8px;' },
          chk,
          h('div', { class: 'sc-clip-title', text: clip.title }),
        ),
        tagsWrap,
        h('div', { class: 'sc-clip-actions' },
          h('button', {
            class: 'sc-btn sc-btn-sm',
            onclick: () => abrirModalEditarCorte(pid, clip),
          }, '✏️ Editar'),
          clip.has_video
            ? h('a', {
                class: 'sc-btn sc-btn-sm',
                href: clip.video_url,
                download: `${clip.title || clip.id}.mp4`,
                style: 'text-decoration:none',
              }, '⬇️ Baixar')
            : null,
          clip.has_video
            ? h('button', {
                class: 'sc-btn sc-btn-primary sc-btn-sm',
                style: 'margin-left:auto',
                onclick: async () => {
                  const disparar = async (confirmado = false) => {
                    try {
                      await apiExt('/publish/batch', {
                        method: 'POST',
                        body: JSON.stringify({
                          project_id: pid,
                          variant_ids: [clip.id],
                          account_id: state.activeAccount,
                          interval_min: 0,
                          confirmado: confirmado,
                        }),
                      });
                      toast('Enviando corte para o TikTok!');
                      navegar('publicador');
                    } catch (e) {
                      if (e.message && e.message.includes('Confirme para continuar')) {
                        if (confirm(e.message)) {
                          await disparar(true);
                        }
                      } else {
                        toast('Erro: ' + e.message);
                      }
                    }
                  };
                  await disparar(false);
                },
              }, 'Publicar')
            : null,
        ),
      ),
    );
    grid.append(card);
  }
}

/* Modal de Edição de Corte */
async function abrirModalEditarCorte(pid, clip) {
  const modal = document.getElementById('dialog-modal');
  const box = document.getElementById('dialog-modal-content');
  modal.style.display = 'flex';
  box.replaceChildren(h('p', null, 'Carregando dados do corte...'));

  try {
    const dados = await apiExt(`/projects/${pid}/clips/${clip.id}/edit`);
    const inpTitulo = h('input', { class: 'sc-input', value: dados.title || clip.title || '' });
    const inpHook = h('input', { class: 'sc-input', value: dados.hook || '' });
    const inpTags = h('input', { class: 'sc-input', value: (dados.tags || []).join(', ') });

    box.replaceChildren(
      h('h3', { style: 'font-size:18px; font-weight:700; margin-bottom:14px;', text: 'Editar Clipe e Legenda de Capa' }),
      h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Título do Clipe (Português)' }), inpTitulo),
      h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Texto de Gancho / Topo (Opcional)' }), inpHook),
      h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Hashtags (separadas por vírgula)' }), inpTags),
      h('div', { style: 'display:flex; justify-content:flex-end; gap:10px; margin-top:18px;' },
        h('button', { class: 'sc-btn', onclick: () => { modal.style.display = 'none'; } }, 'Cancelar'),
        h('button', {
          class: 'sc-btn sc-btn-primary',
          onclick: async () => {
            try {
              await apiExt(`/projects/${pid}/clips/${clip.id}/edit`, {
                method: 'POST',
                body: JSON.stringify({
                  title: inpTitulo.value.trim(),
                  hook: inpHook.value.trim(),
                  tags: inpTags.value.split(',').map((s) => s.trim()).filter(Boolean),
                  rerender: false,
                }),
              });
              toast('Corte atualizado com sucesso!');
              modal.style.display = 'none';
              navegar('projeto', pid);
            } catch (e) {
              toast('Erro ao salvar: ' + e.message);
            }
          },
        }, 'Salvar Alterações'),
      ),
    );
  } catch (e) {
    box.replaceChildren(
      h('p', { text: 'Não foi possível carregar os detalhes: ' + e.message }),
      h('button', { class: 'sc-btn', style: 'margin-top:12px', onclick: () => { modal.style.display = 'none'; } }, 'Fechar'),
    );
  }
}

/* ==========================================================================
   VIEW 3: BIBLIOTECA DE PROJETOS
   ========================================================================== */
async function renderBiblioteca(container) {
  await carregarEstadoInicial();

  const totalProntosGerais = state.projects.reduce(
    (acc, p) => acc + Math.max(0, (p.clips_ready || 0) - (p.clips_published || 0)),
    0,
  );

  container.append(
    h('div', { class: 'sc-page-head', style: 'display:flex; justify-content:space-between; align-items:center;' },
      h('div', null,
        h('h1', { class: 'sc-page-title', text: 'Biblioteca' }),
        h('p', { class: 'sc-page-desc', text: 'Todos os seus vídeos importados e clipes prontos para publicar.' }),
      ),
      h('button', { class: 'sc-btn sc-btn-primary', onclick: () => navegar('criar') }, '+ Nova geração'),
    ),
  );

  if (totalProntosGerais > 0) {
    const selContaGlobal = h('select', {
      class: 'sc-select',
      style: 'width:auto; min-width:180px;',
      onchange: (e) => { state.activeAccount = e.target.value; atualizarSidebarStatus(); },
    }, ...state.accounts.map((acc) =>
      h('option', { value: acc.id, selected: acc.id === state.activeAccount, text: `📱 Conta: ${acc.name}` })
    ));

    const inputIntervaloGlobal = h('input', {
      class: 'sc-input',
      type: 'number',
      min: '0',
      max: '720',
      value: String(state.publishInterval || 30),
      style: 'width:75px',
      title: 'Intervalo em minutos entre postagens',
      oninput: (e) => { state.publishInterval = Math.max(0, parseInt(e.target.value) || 0); },
      onchange: (e) => { state.publishInterval = Math.max(0, parseInt(e.target.value) || 0); },
    });

    const btnPublicarTodosProjetos = h('button', {
      class: 'sc-btn sc-btn-primary',
      onclick: async () => {
        if (!confirm(`Deseja iniciar a publicação em lote de todos os ${totalProntosGerais} corte(s) prontos de todos os projetos na conta "${state.activeAccount}" com intervalo de ${state.publishInterval || 30} min?`)) return;

        const disparar = async (confirmado = false) => {
          try {
            await req('/api/v1/supo/publish/all-pending', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                account_id: state.activeAccount,
                interval_min: state.publishInterval || 30,
                confirmado: confirmado,
              }),
            });
            toast(`Publicação iniciada para todos os ${totalProntosGerais} cortes na conta ${state.activeAccount}!`);
            navegar('publicador');
          } catch (e) {
            if (e.message && e.message.includes('Confirme para continuar')) {
              if (confirm(e.message)) {
                await disparar(true);
              }
            } else {
              toast('Erro ao publicar: ' + (e.message || e));
            }
          }
        };

        await disparar(false);
      },
    }, `🚀 Publicar Todos os Prontos (${totalProntosGerais} cortes)`);

    const bannerGlobal = h('div', {
      class: 'sc-card',
      style: 'margin-bottom:24px; padding:18px 22px; display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; border-left:4px solid var(--primary-bg); background:var(--bg-card); box-shadow:var(--shadow-sm);',
    },
      h('div', null,
        h('div', { style: 'font-weight:600; font-size:14px; margin-bottom:4px; display:flex; align-items:center; gap:8px;' },
          '📦 Publicação em Lote de Todos os Projetos',
          h('span', { class: 'sc-badge-count', text: `${totalProntosGerais} prontos` }),
        ),
        h('div', { style: 'font-size:12.5px; color:var(--text-muted);', text: `Existem ${totalProntosGerais} corte(s) prontos aguardando envio no TikTok entre os seus projetos.` }),
      ),
      h('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' },
        selContaGlobal,
        h('span', { style: 'font-size:12px; color:var(--text-muted);', text: 'Intervalo (min):' }),
        inputIntervaloGlobal,
        btnPublicarTodosProjetos,
      ),
    );

    container.append(bannerGlobal);
  }

  const grid = h('div', { class: 'sc-recent-grid' });
  if (!state.projects.length) {
    grid.append(
      h('div', { class: 'sc-card', style: 'grid-column: 1 / -1; text-align:center; padding:48px;' },
        h('p', { style: 'color:var(--text-muted); margin-bottom:14px;', text: 'Sua biblioteca ainda está vazia.' }),
        h('button', { class: 'sc-btn sc-btn-primary', onclick: () => navegar('criar') }, 'Criar primeiro corte'),
      ),
    );
  } else {
    for (const p of state.projects) {
      const card = h('div', {
        class: 'sc-recent-card',
        onclick: () => navegar('projeto', p.id),
      });
      if (p.has_thumb) {
        card.append(h('img', { class: 'sc-recent-thumb', src: p.thumb_url, alt: p.title || p.name }));
      }
      const metaOverlay = h('div', { class: 'sc-recent-meta', text: `${p.clips_ready}/${p.clips_total} prontos · ${p.clips_published} publicados` });
      const metaCanal = state.projectChannels && state.projectChannels[p.id];
      if (metaCanal) {
        metaOverlay.append(
          h('div', { style: 'margin-top:5px; font-size:11px; color:#10b981; font-weight:700; display:flex; align-items:center; gap:4px;' },
            `📺 ${metaCanal.channel_name}`,
            h('span', { style: 'color:#86efac;' }, `➔ 📱 ${metaCanal.tiktok_account_name || metaCanal.tiktok_account}`)
          )
        );
      }

      card.append(
        h('div', { class: 'sc-recent-overlay' },
          h('div', { class: 'sc-recent-title', text: p.title || p.name }),
          metaOverlay,
        ),
      );
      grid.append(card);
    }
  }
  container.append(grid);
}

/* ==========================================================================
   VIEW 4: PUBLICADOR TIKTOK (CONTAS JÁ CONECTADAS REAPROVEITADAS)
   ========================================================================== */
async function renderPublicador(container) {
  await carregarEstadoInicial();

  const wrap = h('div', { class: 'sc-page-narrow' });
  wrap.append(
    h('div', { class: 'sc-page-head' },
      h('h1', { class: 'sc-page-title', text: 'Publicador TikTok Multi-Contas' }),
      h('p', { class: 'sc-page-desc', text: 'Suas contas e sessões salvas no TikClip foram carregadas automaticamente — não é necessário refazer o login.' }),
    ),
  );

  // Card de Contas Conectadas
  const accList = h('div', { style: 'display:flex; flex-direction:column; gap:10px; margin-bottom:16px;' });
  for (const acc of state.accounts) {
    const isAtiva = acc.id === state.activeAccount;
    accList.append(
      h('div', {
        class: `sc-ai-provider-box ${isAtiva ? 'is-active-box' : ''}`,
        style: 'display:flex; align-items:center; justify-content:space-between; margin:0;',
      },
        h('div', { style: 'display:flex; align-items:center; gap:12px;' },
          h('span', { class: 'sc-dot is-ok' }),
          h('div', null,
            h('div', { style: 'font-weight:700; font-size:14px;', text: `${acc.name} (${acc.id})` }),
            h('div', { style: 'font-size:12px; color:var(--text-muted)', text: 'Sessão de navegador preservada em %APPDATA%\\TikClip' }),
          ),
        ),
        h('div', { style: 'display:flex; gap:8px;' },
          isAtiva
            ? h('span', { class: 'sc-ai-pill-active', text: 'Conta Ativa' })
            : h('button', {
                class: 'sc-btn sc-btn-sm',
                onclick: () => {
                  state.activeAccount = acc.id;
                  atualizarSidebarStatus();
                  renderPublicador(container);
                  toast(`Conta ativa alterada para ${acc.name}`);
                },
              }, 'Usar esta conta'),
          h('button', {
            class: 'sc-btn sc-btn-sm',
            onclick: async () => {
              try {
                toast('Abrindo navegador da conta para conferência...');
                await apiExt(`/accounts/${acc.id}/connect`, { method: 'POST', body: JSON.stringify({}) });
              } catch (e) {
                toast('Aviso: ' + e.message);
              }
            },
          }, 'Abrir / Verificar Login'),
        ),
      ),
    );
  }

  const inpNovaConta = h('input', { class: 'sc-input', placeholder: 'Nome da nova conta (ex.: cortes_humor)', style: 'max-width:280px' });
  const cardContas = h('div', { class: 'sc-card' },
    h('div', { class: 'sc-card-head' },
      h('div', { class: 'sc-card-icon' }, '📱'),
      h('div', null,
        h('div', { class: 'sc-card-title', text: 'Contas do TikTok Cadastradas' }),
        h('div', { class: 'sc-card-sub', text: 'Alterne entre suas contas ou adicione um novo perfil isolado.' }),
      ),
    ),
    accList,
    h('div', { style: 'display:flex; gap:8px; margin-top:12px;' },
      inpNovaConta,
      h('button', {
        class: 'sc-btn sc-btn-primary',
        onclick: async () => {
          const nome = inpNovaConta.value.trim();
          if (!nome) return;
          try {
            await apiExt('/accounts', { method: 'POST', body: JSON.stringify({ name: nome }) });
            toast('Nova conta criada!');
            renderPublicador(container);
          } catch (e) {
            toast('Erro: ' + e.message);
          }
        },
      }, '+ Adicionar Conta'),
    ),
  );

  // Status da Fila de Publicação em Lote
  const pubStatusBox = h('div', { class: 'sc-card' },
    h('div', { class: 'sc-card-head' },
      h('div', { class: 'sc-card-icon' }, '🚀'),
      h('div', null,
        h('div', { class: 'sc-card-title', text: 'Status da Publicação em Lote' }),
        h('div', { class: 'sc-card-sub', text: 'Acompanhe os envios automáticos para o TikTok Studio.' }),
      ),
    ),
    h('div', { id: 'pub-live-box', style: 'font-size:13.5px; color:var(--text-secondary);' }, 'Verificando fila de publicação...'),
  );

  container.replaceChildren(wrap);
  wrap.append(cardContas, pubStatusBox);

  async function pollPub() {
    try {
      const st = await apiExt('/publish/state');
      const box = document.getElementById('pub-live-box');
      if (!box) return;

      const idx = st.index || 0;
      const total = st.total || 0;
      const msg = st.message || (st.running ? 'Publicando corte no TikTok Studio...' : 'Nenhuma publicação em andamento.');
      const logs = st.log || [];
      const account = st.account || state.activeAccount || 'TikTok';

      if (!st.running) {
        box.replaceChildren(
          h('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;' },
            h('span', { style: 'font-weight:600; color:var(--text-secondary);', text: `Último status: ${msg}` }),
            h('button', { class: 'sc-btn sc-btn-sm', onclick: () => navegar('biblioteca') }, 'Escolher clipes na Biblioteca →'),
          ),
          logs.length ? h('div', {
            class: 'sc-log-terminal',
            style: 'background:#0d1117; color:#58a6ff; font-family:Consolas, Monaco, monospace; font-size:12px; padding:12px; border-radius:8px; max-height:220px; overflow-y:auto; line-height:1.5; border:1px solid #30363d;',
          }, ...logs.slice(-25).map((l) => {
            const isSuccess = l.includes('✔') || l.includes('sucesso');
            const isWarn = l.includes('aviso') || l.includes('falhou') || l.includes('interrompida');
            const color = isSuccess ? '#3fb950' : (isWarn ? '#f85149' : '#c9d1d9');
            return h('div', { style: `color:${color}; word-break:break-word;` }, l);
          })) : null,
        );
      } else {
        const isWaiting = msg.toLowerCase().includes('aguardando') || msg.toLowerCase().includes('intervalo');
        const isPublishing = msg.toLowerCase().includes('publicando');
        const isUploading = msg.toLowerCase().includes('arquivo') || msg.toLowerCase().includes('processamento') || msg.toLowerCase().includes('capa');

        let statusText = `🚀 ${msg}`;
        let statusColor = 'var(--ok-color)';

        if (isWaiting) {
          statusText = `⏸️ ${msg}`;
          statusColor = '#d97706';
        } else if (isPublishing) {
          statusText = `⏳ ${msg} — Aguardando os servidores do TikTok processarem o post (leva de 1 a 2 minutos)...`;
          statusColor = '#2563eb';
        } else if (isUploading) {
          statusText = `📤 ${msg}...`;
          statusColor = 'var(--ok-color)';
        }

        const btnParar = h('button', {
          class: 'sc-btn sc-btn-sm',
          style: 'color:var(--err-color); border-color:var(--err-color);',
          onclick: async () => {
            if (!confirm('Deseja realmente interromper a fila de publicação?')) return;
            await apiExt('/publish/stop', { method: 'POST', body: JSON.stringify({}) });
            toast('Parada solicitada. Aguardando encerramento...');
          },
        }, '⏹️ Parar fila');

        const btnPular = isWaiting ? h('button', {
          class: 'sc-btn sc-btn-sm sc-btn-primary',
          style: 'background:#d97706; border-color:#d97706; font-weight:600;',
          title: 'Não esperar os minutos restantes e enviar o próximo corte imediatamente',
          onclick: async () => {
            try {
              await req('/api/v1/supo/publish/skip-wait', { method: 'POST' });
              toast('Pulando intervalo... Enviando próximo corte!');
              pollPub();
            } catch (err) {
              toast('Erro ao pular intervalo: ' + (err.message || err));
            }
          },
        }, '⏩ Pular intervalo e publicar próximo agora') : null;

        const terminalBox = h('div', {
          class: 'sc-log-terminal',
          style: 'background:#0d1117; color:#58a6ff; font-family:Consolas, Monaco, monospace; font-size:12px; padding:12px; border-radius:8px; max-height:240px; overflow-y:auto; line-height:1.5; margin-top:14px; border:1px solid #30363d;',
        }, ...logs.slice(-25).map((l) => {
          const isSuccess = l.includes('✔') || l.includes('sucesso');
          const isWarn = l.includes('aviso') || l.includes('falhou') || l.includes('interrompida');
          const color = isSuccess ? '#3fb950' : (isWarn ? '#f85149' : '#c9d1d9');
          return h('div', { style: `color:${color}; word-break:break-word;` }, l);
        }));

        setTimeout(() => { terminalBox.scrollTop = terminalBox.scrollHeight; }, 50);

        box.replaceChildren(
          h('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; flex-wrap:wrap; gap:8px;' },
            h('div', null,
              h('div', { style: 'font-weight:700; font-size:15px; color:var(--text-main); margin-bottom:2px;' },
                `⏳ Publicando corte ${idx} de ${total} na conta "${account}"`,
              ),
              h('div', { style: `font-size:13px; font-weight:500; color:${statusColor};` },
                statusText,
              ),
            ),
            h('div', { style: 'display:flex; align-items:center; gap:8px;' },
              btnPular,
              btnParar,
            ),
          ),
          terminalBox,
        );
      }
    } catch { /* ignore */ }
  }
  pollPub();
  state.publishTimer = setInterval(pollPub, 2500);
}

/* ==========================================================================
   VIEW 4.5: CANAIS DO YOUTUBE & AUTOMAÇÃO DE CORTES
   ========================================================================== */

/* ==========================================================================
   VIEW: PUBLICADOR YOUTUBE STUDIO (SHORTS & VÍDEOS NORMAIS)
   ========================================================================== */
async function renderYouTubePublisher(container) {
  await carregarEstadoInicial();

  const wrap = h('div', { class: 'sc-page-narrow' });
  wrap.append(
    h('div', { class: 'sc-page-head' },
      h('h1', { class: 'sc-page-title', text: 'Publicador YouTube Studio' }),
      h('p', { class: 'sc-page-desc', text: 'Publique Shorts e Vídeos Normais (com capas em alta definição) diretamente em seus canais sem limites de API.' }),
    ),
  );

  if (!state.ytPubTab) state.ytPubTab = 'shorts';

  const tabs = [
    ['shorts', '⚡ Publicar Shorts'],
    ['normal', '🎥 Publicar Vídeo Normal'],
    ['canais', '👥 Canais do YouTube'],
    ['fila', '📋 Fila & Status ao Vivo'],
  ];

  const tabBar = h('div', { style: 'display:flex; gap:8px; margin-bottom:20px; flex-wrap:wrap;' });
  for (const [id, label] of tabs) {
    tabBar.append(
      h('button', {
        class: `sc-btn ${state.ytPubTab === id ? 'sc-btn-primary' : ''}`,
        onclick: () => {
          state.ytPubTab = id;
          renderYouTubePublisher(container);
        },
      }, label),
    );
  }
  wrap.append(tabBar);

  const body = h('div');
  wrap.append(body);
  container.replaceChildren(wrap);

  function renderSelectCanais() {
    const sel = h('select', { class: 'sc-select', style: 'width:100%; max-width:320px;' });
    if (!state.ytAccounts || !state.ytAccounts.length) {
      sel.append(h('option', { value: 'default' }, 'Canal Principal (Padrão)'));
    } else {
      for (const acc of state.ytAccounts) {
        sel.append(h('option', { value: acc.id, selected: acc.id === (state.activeYtAccount || 'default') }, `${acc.name} (${acc.id})`));
      }
    }
    sel.onchange = (e) => {
      state.activeYtAccount = e.target.value;
    };
    return sel;
  }

  let pollInterval = null;
  async function pollStatus(targetBox) {
    try {
      const res = await apiSupo('/youtube-publisher/status');
      if (!res) return;
      renderStatusBox(targetBox, res);
    } catch (_) {}
  }

  function renderStatusBox(targetBox, st) {
    if (!targetBox) return;
    const isRunning = st.running;
    const logs = st.log || [];
    const msg = st.message || 'Nenhuma publicação recente.';

    const terminal = h('div', {
      class: 'sc-log-terminal',
      style: 'background:#0d1117; color:#58a6ff; font-family:Consolas, Monaco, monospace; font-size:12px; padding:12px; border-radius:8px; max-height:220px; overflow-y:auto; line-height:1.5; margin-top:12px; border:1px solid #30363d;',
    }, ...logs.slice(-30).map((l) => {
      const isSuccess = l.includes('✔') || l.includes('sucesso') || l.includes('concluído');
      const isWarn = l.includes('aviso') || l.includes('falhou') || l.includes('erro') || l.includes('Erro');
      const color = isSuccess ? '#3fb950' : (isWarn ? '#f85149' : '#c9d1d9');
      return h('div', { style: `color:${color}; word-break:break-word;` }, l);
    }));

    setTimeout(() => { terminal.scrollTop = terminal.scrollHeight; }, 30);

    targetBox.replaceChildren(
      h('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; flex-wrap:wrap; gap:8px;' },
        h('div', { style: 'display:flex; align-items:center; gap:8px;' },
          h('span', { class: `sc-dot ${isRunning ? 'is-ok' : ''}` }),
          h('span', { style: 'font-weight:600; font-size:13px;', text: isRunning ? `Publicando (${st.index}/${st.total}): ${msg}` : `Status: ${msg}` }),
        ),
        isRunning ? h('div', { style: 'display:flex; gap:6px;' },
          h('button', {
            class: 'sc-btn sc-btn-sm',
            onclick: async () => {
              await apiSupo('/youtube-publisher/skip-wait', { method: 'POST' });
              toast('Pulando espera...');
            },
          }, '⏩ Pular Intervalo'),
          h('button', {
            class: 'sc-btn sc-btn-sm',
            style: 'color:var(--danger-color);',
            onclick: async () => {
              await apiSupo('/youtube-publisher/stop', { method: 'POST' });
              toast('Fila interrompida.');
            },
          }, '⏹ Parar'),
        ) : null,
      ),
      terminal,
    );
  }

  // ABA 1: SHORTS
  if (state.ytPubTab === 'shorts') {
    const inpVideo = h('input', { class: 'sc-input', placeholder: 'Caminho do arquivo .mp4 no computador (ex.: C:\\meu_video.mp4)' });
    const inpTitle = h('input', { class: 'sc-input', maxlength: 100, placeholder: 'Título do Shorts (ex: Segredo da Produtividade #Shorts)' });
    const txtDesc = h('textarea', { class: 'sc-input', rows: 3, placeholder: 'Descrição do Shorts, hashtags e links...', style: 'resize:vertical;' });
    const inpTags = h('input', { class: 'sc-input', placeholder: 'shorts, viral, podcast, curiosidades' });
    const selVis = h('select', { class: 'sc-select', style: 'max-width:200px;' },
      h('option', { value: 'public' }, 'Público (Recomendado)'),
      h('option', { value: 'unlisted' }, 'Não Listado'),
      h('option', { value: 'private' }, 'Privado'),
    );

    const charCounter = h('span', { style: 'font-size:11px; color:var(--text-muted); margin-left:auto;' }, '0/100');
    inpTitle.oninput = () => {
      charCounter.textContent = `${inpTitle.value.length}/100`;
    };

    const drawerClips = h('div', { style: 'display:none; background:var(--bg-subtle); padding:12px; border-radius:8px; margin-bottom:14px; border:1px solid var(--border-color);' });
    const btnAbrirBiblioteca = h('button', {
      class: 'sc-btn sc-btn-sm',
      style: 'margin-right:8px;',
      onclick: async () => {
        if (drawerClips.style.display !== 'none') {
          drawerClips.style.display = 'none';
          return;
        }
        drawerClips.replaceChildren(h('div', { style: 'font-size:12px; color:var(--text-muted);' }, 'Buscando cortes prontos...'));
        drawerClips.style.display = 'block';

        const listaCortes = [];
        for (const p of state.projects) {
          try {
            const clips = await apiExt(`/projects/${p.id}/clips`);
            for (const c of clips) {
              if (c.status === 'pronto' && c.has_video) {
                listaCortes.push({ proj: p, clip: c });
              }
            }
          } catch (_) {}
        }

        if (!listaCortes.length) {
          drawerClips.replaceChildren(h('div', { style: 'font-size:12px; color:var(--text-muted);' }, 'Nenhum corte pronto encontrado nos projetos. Crie um corte primeiro na aba Criar!'));
          return;
        }

        const grid = h('div', { style: 'display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:10px; max-height:240px; overflow-y:auto; margin-top:8px;' });
        for (const item of listaCortes) {
          grid.append(
            h('div', {
              class: 'sc-card',
              style: 'padding:8px; cursor:pointer; border:1px solid var(--border-color); transition:all 0.15s ease;',
              onclick: () => {
                inpTitle.value = item.clip.title || 'Corte Viral';
                charCounter.textContent = `${inpTitle.value.length}/100`;
                txtDesc.value = item.clip.description || '';
                inpTags.value = item.clip.tags || 'shorts, viral';
                inpVideo.value = `[Projeto] ${item.proj.id} - ${item.clip.id}`;
                inpVideo.dataset.projectId = item.proj.id;
                inpVideo.dataset.variantId = item.clip.id;
                drawerClips.style.display = 'none';
                toast(`Corte "${item.clip.title}" selecionado para envio!`);
              },
            },
              h('div', { style: 'font-weight:600; font-size:12px; margin-bottom:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;', text: item.clip.title || 'Corte' }),
              h('div', { style: 'font-size:11px; color:var(--text-muted);' }, `Projeto: ${item.proj.name || item.proj.id}`),
            ),
          );
        }
        drawerClips.replaceChildren(
          h('div', { style: 'font-weight:600; font-size:12px; color:var(--text-main); margin-bottom:4px;' }, 'Clique em um corte pronto para preencher automaticamente:'),
          grid,
        );
      },
    }, '📚 Escolher da Biblioteca de Cortes');

    const statusCard = h('div', { class: 'sc-card', style: 'margin-top:16px;' });

    const btnEnviarShorts = h('button', {
      class: 'sc-btn sc-btn-primary',
      style: 'font-size:14px; padding:10px 20px;',
      onclick: async () => {
        const valVideo = inpVideo.value.trim();
        if (!valVideo) {
          toast('Por favor, informe o caminho do vídeo ou selecione da biblioteca.');
          return;
        }
        const pid = inpVideo.dataset.projectId;
        const vid = inpVideo.dataset.variantId;
        const payload = {
          account_id: state.activeYtAccount || 'default',
          title: inpTitle.value.trim(),
          description: txtDesc.value.trim(),
          tags: inpTags.value.trim(),
          visibility: selVis.value,
          is_short: true,
        };
        if (pid && vid && valVideo.startsWith('[Projeto]')) {
          payload.project_id = pid;
          payload.variant_id = vid;
        } else {
          payload.video_path = valVideo;
        }

        try {
          btnEnviarShorts.disabled = true;
          toast('Iniciando envio do Shorts no YouTube Studio...');
          await apiSupo('/youtube-publisher/publish-single', { method: 'POST', body: JSON.stringify(payload) });
          toast('Processo iniciado! Acompanhe os logs abaixo.');
          pollStatus(statusCard);
        } catch (e) {
          toast('Erro: ' + (e.message || e));
        } finally {
          btnEnviarShorts.disabled = false;
        }
      },
    }, '🚀 Publicar Shorts no YouTube Agora');

    body.append(
      h('div', { class: 'sc-card' },
        h('div', { class: 'sc-card-head' },
          h('div', { class: 'sc-card-icon' }, '⚡'),
          h('div', null,
            h('div', { class: 'sc-card-title', text: 'Enviar YouTube Shorts' }),
            h('div', { class: 'sc-card-sub', text: 'Vídeos verticais (9:16) com tag #Shorts automática, público-alvo configurado e postagem direta.' }),
          ),
        ),
        h('div', { style: 'display:flex; flex-direction:column; gap:14px;' },
          h('div', null,
            h('label', { class: 'sc-label', text: 'Canal do YouTube de Destino:' }),
            renderSelectCanais(),
          ),
          h('div', null,
            h('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;' },
              h('label', { class: 'sc-label', style: 'margin:0;', text: 'Arquivo de Vídeo (.mp4):' }),
              btnAbrirBiblioteca,
            ),
            drawerClips,
            inpVideo,
          ),
          h('div', null,
            h('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;' },
              h('label', { class: 'sc-label', style: 'margin:0;', text: 'Título do Shorts:' }),
              charCounter,
            ),
            inpTitle,
          ),
          h('div', null,
            h('label', { class: 'sc-label', text: 'Descrição e Créditos:' }),
            txtDesc,
          ),
          h('div', null,
            h('label', { class: 'sc-label', text: 'Tags / Palavras-chave (separadas por vírgula):' }),
            inpTags,
          ),
          h('div', null,
            h('label', { class: 'sc-label', text: 'Visibilidade:' }),
            selVis,
          ),
          h('div', { style: 'margin-top:10px;' },
            btnEnviarShorts,
          ),
        ),
      ),
      statusCard,
    );

    pollStatus(statusCard);
    pollInterval = setInterval(() => pollStatus(statusCard), 2500);
  }

  // ABA 2: VÍDEO NORMAL
  else if (state.ytPubTab === 'normal') {
    const inpVideo = h('input', { class: 'sc-input', placeholder: 'Caminho do arquivo .mp4 no computador (ex.: C:\\meu_video_longo.mp4)' });
    const inpTitle = h('input', { class: 'sc-input', maxlength: 100, placeholder: 'Título do vídeo (até 100 caracteres)' });
    const txtDesc = h('textarea', { class: 'sc-input', rows: 4, placeholder: 'Descrição completa do vídeo, tópicos, links...', style: 'resize:vertical;' });
    const inpCover = h('input', { class: 'sc-input', placeholder: 'Caminho da imagem de capa / thumbnail (.jpg, .png)' });
    const inpTags = h('input', { class: 'sc-input', placeholder: 'palavra-chave 1, palavra-chave 2, podcast, entrevista' });
    const selVis = h('select', { class: 'sc-select', style: 'max-width:200px;' },
      h('option', { value: 'public' }, 'Público (Recomendado)'),
      h('option', { value: 'unlisted' }, 'Não Listado'),
      h('option', { value: 'private' }, 'Privado'),
    );

    const charCounter = h('span', { style: 'font-size:11px; color:var(--text-muted); margin-left:auto;' }, '0/100');
    inpTitle.oninput = () => {
      charCounter.textContent = `${inpTitle.value.length}/100`;
    };

    const btnExtrairCapa = h('button', {
      class: 'sc-btn sc-btn-sm',
      style: 'margin-left:8px;',
      onclick: async () => {
        const vPath = inpVideo.value.trim();
        if (!vPath) {
          toast('Informe o caminho do vídeo primeiro para extrair a capa.');
          return;
        }
        try {
          toast('Extraindo frame em alta qualidade com FFmpeg...');
          const res = await apiSupo('/youtube-publisher/extract-cover', {
            method: 'POST',
            body: JSON.stringify({ video_path: vPath, second: 1.5 }),
          });
          if (res && res.cover_path) {
            inpCover.value = res.cover_path;
            toast('Capa extraída com sucesso ✔');
          }
        } catch (e) {
          toast('Erro ao extrair capa: ' + (e.message || e));
        }
      },
    }, '📸 Extrair Capa com FFmpeg');

    const statusCard = h('div', { class: 'sc-card', style: 'margin-top:16px;' });

    const btnEnviarNormal = h('button', {
      class: 'sc-btn sc-btn-primary',
      style: 'font-size:14px; padding:10px 20px;',
      onclick: async () => {
        const vPath = inpVideo.value.trim();
        if (!vPath) {
          toast('Informe o caminho do vídeo.');
          return;
        }
        const payload = {
          account_id: state.activeYtAccount || 'default',
          video_path: vPath,
          title: inpTitle.value.trim(),
          description: txtDesc.value.trim(),
          tags: inpTags.value.trim(),
          thumbnail_path: inpCover.value.trim() || null,
          visibility: selVis.value,
          is_short: false,
        };

        try {
          btnEnviarNormal.disabled = true;
          toast('Iniciando envio no YouTube Studio...');
          await apiSupo('/youtube-publisher/publish-single', { method: 'POST', body: JSON.stringify(payload) });
          toast('Processo iniciado! Acompanhe os logs abaixo.');
          pollStatus(statusCard);
        } catch (e) {
          toast('Erro: ' + (e.message || e));
        } finally {
          btnEnviarNormal.disabled = false;
        }
      },
    }, '🚀 Publicar Vídeo no YouTube Agora');

    body.append(
      h('div', { class: 'sc-card' },
        h('div', { class: 'sc-card-head' },
          h('div', { class: 'sc-card-icon' }, '🎥'),
          h('div', null,
            h('div', { class: 'sc-card-title', text: 'Enviar Vídeo Normal / Longo' }),
            h('div', { class: 'sc-card-sub', text: 'Upload completo com miniatura personalizada (thumbnail), descrição formatada e metadados no YouTube Studio.' }),
          ),
        ),
        h('div', { style: 'display:flex; flex-direction:column; gap:14px;' },
          h('div', null,
            h('label', { class: 'sc-label', text: 'Canal do YouTube de Destino:' }),
            renderSelectCanais(),
          ),
          h('div', null,
            h('label', { class: 'sc-label', text: 'Arquivo de Vídeo (.mp4, .mkv, .mov):' }),
            inpVideo,
          ),
          h('div', null,
            h('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;' },
              h('label', { class: 'sc-label', style: 'margin:0;', text: 'Título do Vídeo:' }),
              charCounter,
            ),
            inpTitle,
          ),
          h('div', null,
            h('label', { class: 'sc-label', text: 'Descrição Completa:' }),
            txtDesc,
          ),
          h('div', null,
            h('div', { style: 'display:flex; align-items:center; margin-bottom:4px;' },
              h('label', { class: 'sc-label', style: 'margin:0;', text: 'Miniatura / Capa Personalizada (.jpg, .png):' }),
              btnExtrairCapa,
            ),
            inpCover,
          ),
          h('div', null,
            h('label', { class: 'sc-label', text: 'Tags / Palavras-chave:' }),
            inpTags,
          ),
          h('div', null,
            h('label', { class: 'sc-label', text: 'Visibilidade:' }),
            selVis,
          ),
          h('div', { style: 'margin-top:10px;' },
            btnEnviarNormal,
          ),
        ),
      ),
      statusCard,
    );

    pollStatus(statusCard);
    pollInterval = setInterval(() => pollStatus(statusCard), 2500);
  }

  // ABA 3: CANAIS DO YOUTUBE
  else if (state.ytPubTab === 'canais') {
    const accList = h('div', { style: 'display:flex; flex-direction:column; gap:10px; margin-bottom:16px;' });
    for (const acc of (state.ytAccounts || [])) {
      const isAtiva = acc.id === (state.activeYtAccount || 'default');
      accList.append(
        h('div', {
          class: `sc-ai-provider-box ${isAtiva ? 'is-active-box' : ''}`,
          style: 'display:flex; align-items:center; justify-content:space-between; margin:0;',
        },
          h('div', { style: 'display:flex; align-items:center; gap:12px;' },
            h('span', { class: 'sc-dot is-ok' }),
            h('div', null,
              h('div', { style: 'font-weight:700; font-size:14px;', text: `${acc.name} (${acc.id})` }),
              h('div', { style: 'font-size:12px; color:var(--text-muted);', text: 'Perfil de sessão persistente em %APPDATA%\\TikClip' }),
            ),
          ),
          h('div', { style: 'display:flex; gap:8px;' },
            isAtiva
              ? h('span', { class: 'sc-ai-pill-active', text: 'Canal Ativo' })
              : h('button', {
                  class: 'sc-btn sc-btn-sm',
                  onclick: () => {
                    state.activeYtAccount = acc.id;
                    renderYouTubePublisher(container);
                    toast(`Canal ativo alterado para ${acc.name}`);
                  },
                }, 'Usar este canal'),
            h('button', {
              class: 'sc-btn sc-btn-sm',
              onclick: async () => {
                try {
                  toast('Abrindo YouTube Studio no navegador para conferência ou login...');
                  await apiSupo(`/youtube-publisher/accounts/${acc.id}/connect`, { method: 'POST' });
                } catch (e) {
                  toast('Aviso: ' + (e.message || e));
                }
              },
            }, 'Abrir / Conectar Canal'),
            acc.id !== 'default' ? h('button', {
              class: 'sc-btn sc-btn-sm',
              style: 'color:var(--danger-color);',
              onclick: async () => {
                if (!confirm(`Deseja remover o canal "${acc.name}"?`)) return;
                try {
                  await apiSupo(`/youtube-publisher/accounts/${acc.id}`, { method: 'DELETE' });
                  toast('Canal removido.');
                  renderYouTubePublisher(container);
                } catch (e) {
                  toast('Erro: ' + (e.message || e));
                }
              },
            }, 'Excluir') : null,
          ),
        ),
      );
    }

    const inpNovoCanal = h('input', { class: 'sc-input', placeholder: 'Nome do novo canal (ex.: Cortes do Podcast)', style: 'max-width:280px;' });
    body.append(
      h('div', { class: 'sc-card' },
        h('div', { class: 'sc-card-head' },
          h('div', { class: 'sc-card-icon' }, '👥'),
          h('div', null,
            h('div', { class: 'sc-card-title', text: 'Canais do YouTube Cadastrados' }),
            h('div', { class: 'sc-card-sub', text: 'Cada canal opera em um perfil de navegador isolado, permitindo gerenciar vários canais sem misturar logins.' }),
          ),
        ),
        accList,
        h('div', { style: 'display:flex; gap:8px; margin-top:12px;' },
          inpNovoCanal,
          h('button', {
            class: 'sc-btn sc-btn-primary',
            onclick: async () => {
              const nome = inpNovoCanal.value.trim();
              if (!nome) return;
              try {
                await apiSupo('/youtube-publisher/accounts', { method: 'POST', body: JSON.stringify({ name: nome }) });
                toast('Novo canal cadastrado com sucesso!');
                renderYouTubePublisher(container);
              } catch (e) {
                toast('Erro: ' + (e.message || e));
              }
            },
          }, '+ Adicionar Canal'),
        ),
      ),
    );
  }

  // ABA 4: FILA & STATUS EM TEMPO REAL
  else if (state.ytPubTab === 'fila') {
    const statusCard = h('div', { class: 'sc-card' });
    const inpInterval = h('input', { class: 'sc-input', type: 'number', min: 1, value: 15, style: 'max-width:100px;' });

    const btnLoteTodosCortes = h('button', {
      class: 'sc-btn sc-btn-primary',
      onclick: async () => {
        btnLoteTodosCortes.disabled = true;
        toast('Buscando todos os cortes prontos nos projetos...');
        const items = [];
        for (const p of state.projects) {
          try {
            const clips = await apiExt(`/projects/${p.id}/clips`);
            for (const c of clips) {
              if (c.status === 'pronto' && c.has_video) {
                const st = await apiExt(`/projects/${p.id}`);
                const jobs = (st.jobs || []).reduce((acc, j) => { acc[j.job_id] = j; return acc; }, {});
                const variant = (st.output_variants || []).find((v) => v.id === c.id) || {};
                const job = jobs[variant.render_job_id];
                if (job && job.output_files && job.output_files.length) {
                  items.push({
                    video_path: job.output_files[0],
                    title: c.title || 'Corte TikClip #Shorts',
                    description: c.description || '',
                    tags: c.tags || 'shorts, viral',
                    is_short: true,
                  });
                }
              }
            }
          } catch (_) {}
        }

        if (!items.length) {
          toast('Nenhum corte pronto com arquivo de vídeo encontrado.');
          btnLoteTodosCortes.disabled = false;
          return;
        }

        if (!confirm(`Deseja iniciar a publicação em lote de ${items.length} corte(s) no YouTube com intervalo de ${inpInterval.value} minuto(s)?`)) {
          btnLoteTodosCortes.disabled = false;
          return;
        }

        try {
          await apiSupo('/youtube-publisher/publish-batch', {
            method: 'POST',
            body: JSON.stringify({
              items,
              account_id: state.activeYtAccount || 'default',
              interval_min: parseInt(inpInterval.value, 10) || 15,
              visibility: 'public',
            }),
          });
          toast(`Fila iniciada com ${items.length} cortes!`);
          pollStatus(statusCard);
        } catch (e) {
          toast('Erro ao iniciar fila: ' + (e.message || e));
        } finally {
          btnLoteTodosCortes.disabled = false;
        }
      },
    }, '🚀 Publicar Todos os Cortes Prontos no YouTube');

    body.append(
      h('div', { class: 'sc-card', style: 'margin-bottom:16px;' },
        h('div', { class: 'sc-card-head' },
          h('div', { class: 'sc-card-icon' }, '📋'),
          h('div', null,
            h('div', { class: 'sc-card-title', text: 'Disparador de Publicação em Lote' }),
            h('div', { class: 'sc-card-sub', text: 'Publique múltiplos cortes em sequência no YouTube com intervalo seguro anti-spam.' }),
          ),
        ),
        h('div', { style: 'display:flex; align-items:center; gap:12px; flex-wrap:wrap;' },
          h('div', { style: 'display:flex; align-items:center; gap:8px;' },
            h('label', { class: 'sc-label', style: 'margin:0;', text: 'Intervalo entre vídeos (min):' }),
            inpInterval,
          ),
          btnLoteTodosCortes,
        ),
      ),
      statusCard,
    );

    pollStatus(statusCard);
    pollInterval = setInterval(() => pollStatus(statusCard), 2500);
  }

  if (state.ytPubTimer) clearInterval(state.ytPubTimer);
  state.ytPubTimer = pollInterval;
}


async function renderCanais(container) {
  container.replaceChildren();

  const loadingWrap = h('div', { class: 'sc-card', style: 'padding:32px; text-align:center; color:var(--text-muted);' },
    'Carregando canais do YouTube e automações...'
  );
  container.append(loadingWrap);

  try {
    const data = await apiSupo('/channels');
    state.channels = data.channels || [];
    state.projectChannels = data.project_meta || {};
    atualizarSidebarStatus();
  } catch (e) {
    toast('Erro ao buscar canais: ' + (e.message || e));
  }

  container.replaceChildren();

  // Cabeçalho da página
  const btnCadastrar = h('button', {
    class: 'sc-btn sc-btn-primary',
    style: 'display:flex; align-items:center; gap:6px;',
    onclick: () => abrirModalCanal(),
  },
    h('span', { style: 'font-size:16px; font-weight:bold;' }, '+'),
    'Cadastrar Canal'
  );

  const btnChecarTodos = h('button', {
    class: 'sc-btn',
    style: 'display:flex; align-items:center; gap:6px;',
    onclick: async () => {
      btnChecarTodos.disabled = true;
      btnChecarTodos.textContent = '🔄 Verificando...';
      try {
        const res = await apiSupo('/channels/check-now', { method: 'POST' });
        const novos = (res.new_videos_imported || []).length;
        if (novos > 0) {
          toast(`Sucesso! ${novos} novo(s) vídeo(s) importado(s) e enviado(s) para corte!`);
        } else {
          toast('Varredura concluída. Nenhum vídeo novo postado nos canais.');
        }
        await renderCanais(container);
      } catch (e) {
        toast('Erro ao verificar canais: ' + (e.message || e));
      } finally {
        btnChecarTodos.disabled = false;
        btnChecarTodos.textContent = '🔄 Checar Todos Agora';
      }
    },
  }, '🔄 Checar Todos Agora');

  const pageHead = h('div', { class: 'sc-page-head', style: 'display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px; margin-bottom:24px;' },
    h('div', null,
      h('h1', { class: 'sc-page-title' }, '📺 Monitoramento de Canais do YouTube'),
      h('p', { class: 'sc-page-sub' }, 'Cadastre canais para importar e cortar vídeos novos automaticamente. Cada canal publica exclusivamente na conta do TikTok que você escolher.'),
    ),
    h('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' },
      btnChecarTodos,
      btnCadastrar,
    ),
  );
  container.append(pageHead);

  // Banner Informativo de Proteção de Contas
  const bannerInfo = h('div', {
    class: 'sc-card',
    style: 'padding:14px 18px; margin-bottom:20px; background:linear-gradient(135deg, rgba(34,197,94,0.08), rgba(59,130,246,0.05)); border:1px solid rgba(34,197,94,0.3); display:flex; align-items:center; justify-content:space-between; gap:14px; flex-wrap:wrap;',
  },
    h('div', { style: 'display:flex; align-items:center; gap:12px;' },
      h('span', { style: 'font-size:24px;' }, '🛡️'),
      h('div', null,
        h('strong', { style: 'font-size:13.5px; color:var(--text-main); display:block; margin-bottom:2px;' }, 'Proteção e Isolamento Rigoroso de Contas do TikTok'),
        h('span', { style: 'font-size:12px; color:var(--text-muted);' }, 'Cada canal do YouTube cadastrado é amarrado à conta do TikTok que você definir. Vídeos de um canal nunca sobem na conta errada.'),
      ),
    ),
    h('span', { class: 'sc-chip', style: 'background:#dcfce7; color:#15803d; font-weight:700; border:1px solid #86efac;' }, `${(state.channels || []).length} canal(is) configurado(s)`),
  );
  container.append(bannerInfo);

  // Grid de Canais
  const channelsList = state.channels || [];
  if (!channelsList.length) {
    const emptyBox = h('div', {
      class: 'sc-card',
      style: 'text-align:center; padding:54px 20px; display:flex; flex-direction:column; align-items:center; gap:16px;',
    },
      h('div', { style: 'font-size:46px;' }, '📺'),
      h('h3', { style: 'font-size:17px; font-weight:700; margin:0;' }, 'Nenhum canal do YouTube monitorado ainda'),
      h('p', { style: 'max-width:520px; font-size:13.5px; color:var(--text-muted); line-height:1.5; margin:0;' },
        'Cadastre seu primeiro canal (por exemplo, ',
        h('b', null, '@tiagotcar'),
        ') e selecione a conta do TikTok desejada. O TikClip Web verificará periodicamente se há novos vídeos e iniciará o corte dos momentos automaticamente.'
      ),
      h('button', {
        class: 'sc-btn sc-btn-primary',
        style: 'margin-top:8px; padding:10px 22px; font-weight:700;',
        onclick: () => abrirModalCanal(),
      }, '+ Cadastrar Primeiro Canal'),
    );
    container.append(emptyBox);
  } else {
    const grid = h('div', { class: 'sc-channels-grid' });

    for (const ch of channelsList) {
      const card = h('div', { class: 'sc-channel-card' });

      // Topo: Avatar + Nome + Status
      const avatarSrc = ch.avatar_url || 'https://www.youtube.com/s/desktop/28b08f86/img/favicon_144x144.png';
      const avatarImg = h('img', {
        class: 'sc-channel-avatar',
        src: avatarSrc,
        alt: ch.name,
        onerror: (e) => { e.target.style.display = 'none'; },
      });

      const statusBadge = h('span', {
        class: 'sc-chip',
        style: ch.active
          ? 'background:#dcfce7; color:#15803d; border:1px solid #86efac; font-weight:700; cursor:pointer;'
          : 'background:#f3f4f6; color:#6b7280; border:1px solid #d1d5db; font-weight:700; cursor:pointer;',
        title: 'Clique para pausar ou retomar o monitoramento',
        onclick: async () => {
          try {
            await apiSupo(`/channels/${ch.id}`, {
              method: 'PUT',
              body: JSON.stringify({ active: !ch.active }),
            });
            toast(ch.active ? 'Canal pausado.' : 'Canal retomado!');
            await renderCanais(container);
          } catch (e) {
            toast('Erro ao atualizar: ' + (e.message || e));
          }
        },
      }, ch.active ? '🟢 Monitorando' : '⏸️ Pausado');

      const header = h('div', { class: 'sc-channel-header' },
        avatarImg,
        h('div', { class: 'sc-channel-info' },
          h('div', { class: 'sc-channel-name' },
            ch.name,
          ),
          h('div', { class: 'sc-channel-handle' },
            ch.handle || '',
            ch.channel_url ? h('a', { href: ch.channel_url, target: '_blank', title: 'Abrir no YouTube' }, '↗') : null,
          ),
        ),
        statusBadge,
      );
      card.append(header);

      // Caixa em Destaque: Conta do TikTok Vinculada
      const accountBox = h('div', { class: 'sc-channel-account-box' },
        h('div', null,
          h('div', { class: 'sc-channel-account-label' }, '📱 Conta TikTok Vinculada'),
          h('div', { class: 'sc-channel-account-val' },
            '🔒 ',
            ch.tiktok_account_name || ch.tiktok_account,
          ),
        ),
        h('span', {
          style: 'font-size:11.5px; font-weight:600; background:#ffffff; color:#15803d; border:1px solid rgba(34,197,94,0.4); padding:3px 8px; border-radius:6px;',
        }, 'Exclusiva'),
      );
      card.append(accountBox);

      // Linhas de Metadados (Modo, Intervalo, Hashtags)
      const hashtagsList = Array.isArray(ch.hashtags) ? ch.hashtags : [];
      const metaRows = h('div', { class: 'sc-channel-meta-rows' },
        h('div', { class: 'sc-channel-meta-item' },
          h('span', null, 'Modo de Envio:'),
          h('b', { style: ch.auto_publish ? 'color:#15803d;' : 'color:var(--text-main);' },
            ch.auto_publish ? '🚀 Publicar automático ao concluir' : '📁 Apenas gerar cortes (aguardar aprovação)'
          ),
        ),
        h('div', { class: 'sc-channel-meta-item' },
          h('span', null, 'Intervalo entre posts:'),
          h('b', null, `${ch.interval_min || 30} minutos`),
        ),
        hashtagsList.length ? h('div', { style: 'margin-top:2px;' },
          h('span', { style: 'font-size:11.5px; color:var(--text-muted); display:block; margin-bottom:4px;' }, 'Hashtags automáticas:'),
          h('div', { style: 'display:flex; flex-wrap:wrap; gap:4px;' },
            ...hashtagsList.map((tag) => h('span', { class: 'sc-hashtag-chip', style: 'padding:2px 8px; font-size:11px;' }, tag.startsWith('#') ? tag : '#' + tag))
          ),
        ) : null,
      );
      card.append(metaRows);

      // Função para gerar cortes do último vídeo manualmente sob demanda
      const dispararGeracaoUltimoVideo = async (btnElement) => {
        if (!ch.last_video_url) {
          toast('Nenhum vídeo recente encontrado para este canal.');
          return;
        }
        btnElement.disabled = true;
        const textoOriginal = btnElement.textContent;
        btnElement.textContent = '⏳ Importando...';
        toast(`Importando "${ch.last_video_title || 'vídeo'}" e iniciando geração de cortes...`);

        try {
          const fd = new FormData();
          fd.append('goal', 'auto');
          fd.append('language', 'source');
          fd.append('aspect', 'portrait');
          fd.append('duration', '60');
          fd.append('platforms', 'tiktok');
          fd.append('auto_start', 'true');
          fd.append('brand_outro_enabled', 'false');
          fd.append('name', `${ch.name} - ${ch.last_video_title || 'Cortes'}`.slice(0, 180));
          fd.append('url', ch.last_video_url);

          const res = await req('/api/v1/studio/import', { method: 'POST', body: fd });
          const pid = res.project_id;

          // 1. Anexar hashtags do canal ao projeto
          if (ch.hashtags && ch.hashtags.length) {
            await apiSupo(`/projects/${pid}/hashtags`, {
              method: 'POST',
              body: JSON.stringify({ hashtags: ch.hashtags }),
            }).catch(() => null);
          }

          // 2. Salvar metadados de vínculo de conta para este projeto
          state.projectChannels = state.projectChannels || {};
          state.projectChannels[pid] = {
            channel_id: ch.id,
            channel_name: ch.name,
            channel_handle: ch.handle,
            tiktok_account: ch.tiktok_account,
            tiktok_account_name: ch.tiktok_account_name || ch.tiktok_account,
            auto_publish: ch.auto_publish,
            interval_min: ch.interval_min || 30,
            video_title: ch.last_video_title,
          };

          // 3. Atualizar histórico do canal
          ch.history = ch.history || [];
          ch.history.unshift({
            video_id: ch.last_video_id || '',
            video_title: ch.last_video_title || '',
            video_url: ch.last_video_url || '',
            project_id: pid,
            imported_at: Math.floor(Date.now() / 1000),
            status: 'processing',
            tiktok_account: ch.tiktok_account,
          });

          toast(`Sucesso! Cortes sendo gerados para "${ch.name}". Abrindo projeto...`);
          await carregarEstadoInicial();
          navegar('projeto', pid);
        } catch (err) {
          toast('Erro ao importar vídeo: ' + (err.message || err));
          btnElement.disabled = false;
          btnElement.textContent = textoOriginal;
        }
      };

      // Caixa do Último Vídeo com Botão Imediato
      if (ch.last_video_title) {
        const btnCortarDeste = h('button', {
          class: 'sc-btn sc-btn-sm sc-btn-primary',
          style: 'padding: 6px 12px; font-weight: 700; white-space: nowrap; display: flex; align-items: center; gap: 5px; flex-shrink: 0;',
          title: `Gerar projeto e cortes deste vídeo agora`,
          onclick: (e) => { e.stopPropagation(); dispararGeracaoUltimoVideo(btnCortarDeste); },
        }, '⚡ Gerar Cortes Agora');

        const videoBox = h('div', { class: 'sc-channel-video-box', style: 'display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;' },
          h('div', { class: 'sc-channel-video-details' },
            h('div', { style: 'font-size:10.5px; text-transform:uppercase; font-weight:700; color:var(--text-muted); margin-bottom:3px;' }, 'Último vídeo publicado:'),
            h('div', { class: 'sc-channel-video-title', title: ch.last_video_title }, ch.last_video_title),
            h('div', { class: 'sc-channel-video-sub' },
              ch.last_video_url ? h('a', { href: ch.last_video_url, target: '_blank', style: 'color:var(--primary-bg); text-decoration:none;' }, 'Assistir no YouTube ↗') : null,
            ),
          ),
          btnCortarDeste,
        );
        card.append(videoBox);
      }

      // Botões de Ação do Card
      const btnGerarUltimoAcao = h('button', {
        class: 'sc-btn sc-btn-sm sc-btn-primary',
        style: 'font-weight: 700; display: flex; align-items: center; gap: 4px;',
        title: 'Gerar cortes deste último vídeo publicado agora mesmo',
        onclick: () => dispararGeracaoUltimoVideo(btnGerarUltimoAcao),
      }, '🎬 Gerar do Último Vídeo');

      const btnChecarCanal = h('button', {
        class: 'sc-btn sc-btn-sm',
        title: 'Verificar se há novos vídeos agora',
        onclick: async () => {
          btnChecarCanal.disabled = true;
          btnChecarCanal.textContent = 'Buscando...';
          try {
            const res = await apiSupo(`/channels/${ch.id}/check`, { method: 'POST' });
            if (res.new_videos_count > 0) {
              toast(`Novo vídeo importado com sucesso para ${ch.name}!`);
            } else {
              toast(`Nenhum vídeo novo em ${ch.name}.`);
            }
            await renderCanais(container);
          } catch (e) {
            toast('Erro ao checar: ' + (e.message || e));
          } finally {
            btnChecarCanal.disabled = false;
            btnChecarCanal.textContent = '🔍 Checar';
          }
        },
      }, '🔍 Checar');

      const btnEditar = h('button', {
        class: 'sc-btn sc-btn-sm',
        onclick: () => abrirModalCanal(ch),
      }, '⚙️ Configurar');

      const btnRemover = h('button', {
        class: 'sc-btn sc-btn-sm',
        style: 'color:var(--err-color); margin-left:auto;',
        onclick: async () => {
          if (!confirm(`Deseja remover o monitoramento do canal "${ch.name}"?`)) return;
          try {
            await apiSupo(`/channels/${ch.id}`, { method: 'DELETE' });
            toast('Canal removido com sucesso.');
            await renderCanais(container);
          } catch (e) {
            toast('Erro ao remover: ' + (e.message || e));
          }
        },
      }, '🗑️');

      const actions = h('div', { class: 'sc-channel-actions' },
        btnGerarUltimoAcao,
        btnChecarCanal,
        btnEditar,
        btnRemover,
      );
      card.append(actions);

      grid.append(card);
    }
    container.append(grid);
  }

  // Histórico de Vídeos Importados Automaticamente
  const todosHistoricos = [];
  for (const ch of (state.channels || [])) {
    for (const hItem of (ch.history || [])) {
      todosHistoricos.push({ ...hItem, channel_name: ch.name, channel_handle: ch.handle });
    }
  }
  todosHistoricos.sort((a, b) => (b.imported_at || 0) - (a.imported_at || 0));

  if (todosHistoricos.length > 0) {
    const histSection = h('div', { class: 'sc-card', style: 'margin-top:28px;' },
      h('div', { class: 'sc-card-head' },
        h('div', { class: 'sc-card-icon' }, '📜'),
        h('div', null,
          h('div', { class: 'sc-card-title' }, 'Histórico de Vídeos Importados Automaticamente'),
          h('div', { class: 'sc-card-sub' }, 'Lista dos vídeos que o robô já detectou e transformou em projetos.'),
        ),
      ),
      h('div', { style: 'display:flex; flex-direction:column; gap:10px;' },
        ...todosHistoricos.slice(0, 15).map((item) => {
          return h('div', {
            class: 'sc-ai-provider-box',
            style: 'margin:0; padding:12px 16px; display:flex; align-items:center; justify-content:space-between; gap:14px; flex-wrap:wrap;',
          },
            h('div', { style: 'flex:1; min-width:240px;' },
              h('div', { style: 'font-weight:700; font-size:13.5px; color:var(--text-main); margin-bottom:3px;' }, item.video_title),
              h('div', { style: 'font-size:12px; color:var(--text-muted); display:flex; gap:10px; align-items:center; flex-wrap:wrap;' },
                h('span', null, `📺 ${item.channel_name}`),
                h('span', { style: 'color:#15803d; font-weight:600;' }, `📱 ${item.tiktok_account}`),
                h('span', null, `⏱️ ${new Date((item.imported_at || 0) * 1000).toLocaleString('pt-BR')}`),
              ),
            ),
            item.project_id ? h('button', {
              class: 'sc-btn sc-btn-sm sc-btn-primary',
              onclick: () => navegar('projeto', item.project_id),
            }, 'Abrir Projeto ➔') : null,
          );
        }),
      ),
    );
    container.append(histSection);
  }
}

function abrirModalCanal(canalParaEditar = null) {
  const isEdit = !!canalParaEditar;

  const backdrop = h('div', { class: 'sc-modal-backdrop' });

  const inputUrl = h('input', {
    class: 'sc-input',
    type: 'text',
    placeholder: 'Ex: https://www.youtube.com/@tiagotcar ou @tiagotcar',
    value: isEdit ? (canalParaEditar.channel_url || canalParaEditar.handle || '') : '',
    disabled: isEdit,
  });

  const selectConta = h('select', {
    class: 'sc-select',
    style: 'font-weight:600;',
  }, ...state.accounts.map((acc) =>
    h('option', {
      value: acc.id,
      selected: isEdit ? canalParaEditar.tiktok_account === acc.id : acc.id === state.activeAccount,
      text: `📱 ${acc.name} (${acc.id})`,
    })
  ));

  const inputHashtags = h('input', {
    class: 'sc-input',
    type: 'text',
    placeholder: 'Ex: #carros #gasmonkey #tcar #automotivo',
    value: isEdit && canalParaEditar.hashtags ? (Array.isArray(canalParaEditar.hashtags) ? canalParaEditar.hashtags.join(' ') : canalParaEditar.hashtags) : '',
  });

  const chkAutoPublish = h('input', {
    type: 'checkbox',
    style: 'cursor:pointer; width:16px; height:16px;',
    checked: isEdit ? !!canalParaEditar.auto_publish : false,
  });

  const inputIntervalo = h('input', {
    class: 'sc-input',
    type: 'number',
    min: '1',
    max: '720',
    value: isEdit ? String(canalParaEditar.interval_min || 30) : '30',
    style: 'width:90px;',
  });

  const chkImportNow = h('input', {
    type: 'checkbox',
    style: 'cursor:pointer; width:16px; height:16px;',
    checked: !isEdit,
  });

  const btnSalvar = h('button', {
    class: 'sc-btn sc-btn-primary',
    style: 'padding:9px 20px; font-weight:700;',
    onclick: async () => {
      const url = inputUrl.value.trim();
      const accountId = selectConta.value;
      const selectedAcc = state.accounts.find((a) => a.id === accountId);
      const accountName = selectedAcc ? selectedAcc.name : accountId;

      if (!isEdit && !url) {
        toast('Informe o link ou @ do canal no YouTube.');
        inputUrl.focus();
        return;
      }

      if (!accountId) {
        toast('Selecione a conta do TikTok vinculada.');
        return;
      }

      btnSalvar.disabled = true;
      btnSalvar.textContent = isEdit ? 'Salvando...' : '🔍 Buscando Canal no YouTube...';

      try {
        const tags = inputHashtags.value.replace(/,/g, ' ').split(/\s+/).map((t) => t.trim()).filter(Boolean);

        if (isEdit) {
          await apiSupo(`/channels/${canalParaEditar.id}`, {
            method: 'PUT',
            body: JSON.stringify({
              tiktok_account: accountId,
              tiktok_account_name: accountName,
              hashtags: tags,
              auto_publish: chkAutoPublish.checked,
              interval_min: parseInt(inputIntervalo.value) || 30,
            }),
          });
          toast('Canal atualizado com sucesso!');
        } else {
          const res = await apiSupo('/channels', {
            method: 'POST',
            body: JSON.stringify({
              url: url,
              tiktok_account: accountId,
              tiktok_account_name: accountName,
              hashtags: tags,
              auto_publish: chkAutoPublish.checked,
              interval_min: parseInt(inputIntervalo.value) || 30,
              import_latest_now: chkImportNow.checked,
            }),
          });
          toast(`Canal "${res.channel.name}" cadastrado com sucesso e vinculado à conta "${accountName}"!`);
          if (res.imported_now) {
            toast('Vídeo mais recente importado! O corte dos momentos já começou.');
          }
        }
        backdrop.remove();
        const main = document.getElementById('main-view');
        await renderCanais(main);
      } catch (e) {
        toast('Erro: ' + (e.message || e));
        btnSalvar.disabled = false;
        btnSalvar.textContent = isEdit ? 'Salvar Alterações' : 'Salvar e Iniciar Monitoramento';
      }
    },
  }, isEdit ? 'Salvar Alterações' : 'Salvar e Iniciar Monitoramento');

  const btnCancelar = h('button', {
    class: 'sc-btn',
    onclick: () => backdrop.remove(),
  }, 'Cancelar');

  const modalBox = h('div', { class: 'sc-modal-box' },
    h('div', null,
      h('div', { class: 'sc-modal-title' },
        '📺 ',
        isEdit ? `Configurações do Canal "${canalParaEditar.name}"` : 'Cadastrar Canal do YouTube',
      ),
      h('div', { class: 'sc-modal-sub' },
        isEdit
          ? 'Altere a conta do TikTok vinculada, as hashtags padrão ou a publicação automática.'
          : 'Digite o link ou @ do canal. Quando um novo vídeo sair, o TikClip Web importa e corta automaticamente.'
      ),
    ),

    // Campo URL / Handle
    h('div', { class: 'sc-form-group' },
      h('label', { class: 'sc-form-label' }, '🔗 Link ou @ do Canal no YouTube'),
      inputUrl,
      h('span', { class: 'sc-form-hint' }, 'Exemplos: https://www.youtube.com/@tiagotcar ou simplesmente @tiagotcar'),
    ),

    // Campo CONTA TIKTOK (Destaque)
    h('div', { class: 'sc-form-group sc-highlight-field' },
      h('label', { class: 'sc-form-label', style: 'color:#15803d;' }, '📱 Conta do TikTok para Publicação (Obrigatório)'),
      selectConta,
      h('span', { class: 'sc-form-hint', style: 'color:#166534; font-weight:500; margin-top:4px;' },
        '🔒 Regra de Segurança: todos os vídeos importados deste canal serão publicados EXCLUSIVAMENTE nesta conta para evitar postagens acidentais.'
      ),
    ),

    // Campo Hashtags
    h('div', { class: 'sc-form-group' },
      h('label', { class: 'sc-form-label' }, '🏷️ Hashtags Padrão do Canal'),
      inputHashtags,
      h('span', { class: 'sc-form-hint' }, 'Estas hashtags serão adicionadas automaticamente a todos os cortes gerados deste canal.'),
    ),

    // Configurações de Publicação Automática
    h('div', { class: 'sc-form-group', style: 'background:var(--bg-subtle); padding:14px; border-radius:var(--radius-md); border:1px solid var(--border-color);' },
      h('label', { style: 'display:flex; align-items:center; gap:10px; cursor:pointer; font-weight:700; font-size:13.5px;' },
        chkAutoPublish,
        '🚀 Publicar automaticamente no TikTok assim que os cortes ficarem prontos',
      ),
      h('div', { style: 'font-size:12px; color:var(--text-muted); margin:4px 0 10px 26px;' },
        'Se ativado, o robô envia os cortes para a conta vinculada sem precisar de intervenção manual. Se desativado, os cortes ficam prontos na Biblioteca para você revisar.'
      ),
      h('div', { style: 'display:flex; align-items:center; gap:8px; margin-left:26px;' },
        h('span', { style: 'font-size:12.5px; color:var(--text-muted);' }, 'Intervalo entre postagens:'),
        inputIntervalo,
        h('span', { style: 'font-size:12.5px; color:var(--text-muted);' }, 'minutos'),
      ),
    ),

    // Importar mais recente agora? (somente no cadastro novo)
    !isEdit ? h('div', { class: 'sc-form-group' },
      h('label', { style: 'display:flex; align-items:center; gap:10px; cursor:pointer; font-size:13px; font-weight:600;' },
        chkImportNow,
        '📥 Já importar e gerar cortes do vídeo mais recente agora mesmo',
      ),
    ) : null,

    // Rodapé
    h('div', { class: 'sc-modal-foot' },
      btnCancelar,
      btnSalvar,
    ),
  );

  backdrop.append(modalBox);
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) backdrop.remove();
  });

  document.body.append(backdrop);
  if (!isEdit) inputUrl.focus();
}

/* ==========================================================================
   VIEW: VÍDEOS DO TIKTOK (INTEGRAÇÃO TIKTOK-DOWNLOADER-WEB)
   ========================================================================== */

function obterContaCanalTikTok(folder) {
  if (!folder) return state.activeAccount || 'conta_meuscortes';
  try {
    const raw = localStorage.getItem('sc-tiktok-channel-accounts');
    const map = raw ? JSON.parse(raw) : {};
    const f1 = folder.trim();
    const f2 = f1.replace('@', '').toLowerCase();
    const saved = map[f1] || map[f2] || map[`@${f2}`];
    if (saved && state.accounts.some((a) => a.id === saved)) {
      return saved;
    }
  } catch {}
  return state.activeAccount || 'conta_meuscortes';
}

function salvarContaCanalTikTok(folder, accountId) {
  if (!folder || !accountId) return;
  try {
    const raw = localStorage.getItem('sc-tiktok-channel-accounts');
    const map = raw ? JSON.parse(raw) : {};
    const f1 = folder.trim();
    const f2 = f1.replace('@', '').toLowerCase();
    map[f1] = accountId;
    map[f2] = accountId;
    map[`@${f2}`] = accountId;
    localStorage.setItem('sc-tiktok-channel-accounts', JSON.stringify(map));
  } catch {}
}

async function renderTikTokDownloads(container) {
  if (state.selectedTikTokCreator) {
    await renderTikTokCreatorVideos(container, state.selectedTikTokCreator);
  } else {
    await renderTikTokCreatorsList(container);
  }
}

async function renderTikTokCreatorsList(container) {
  container.replaceChildren();

  const loadingWrap = h('div', { class: 'sc-card', style: 'padding:32px; text-align:center; color:var(--text-muted);' },
    'Carregando canais e vídeos baixados do TikTok...'
  );
  container.append(loadingWrap);

  let creators = [];
  let totalVideos = 0;
  try {
    const data = await apiSupo('/tiktok-downloads/creators');
    creators = data.creators || [];
    totalVideos = data.total_videos || 0;
    state.tiktokCreators = creators;
    atualizarSidebarStatus();
  } catch (e) {
    console.warn('Erro ao buscar vídeos do TikTok:', e);
    creators = state.tiktokCreators || [];
    totalVideos = creators.reduce((acc, c) => acc + (c.video_count || 0), 0);
    if (!creators.length) {
      toast('Erro ao buscar vídeos do TikTok: ' + (e.message || e));
    }
  }

  container.replaceChildren();

  // Cabeçalho da página
  const btnAtualizar = h('button', {
    class: 'sc-btn',
    style: 'display:flex; align-items:center; gap:6px;',
    onclick: () => renderTikTokCreatorsList(container),
  },
    h('span', null, '🔄'),
    'Atualizar Lista'
  );

  const head = h('div', { class: 'sc-page-head', style: 'display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;' },
    h('div', null,
      h('div', { style: 'display:flex; align-items:center; gap:10px; margin-bottom:4px;' },
        h('h1', { class: 'sc-page-title', style: 'margin:0;' }, 'Vídeos do TikTok'),
        h('span', { class: 'sc-tiktok-badge' }, 'tiktok-downloader-web'),
      ),
      h('p', { class: 'sc-page-desc' }, 'Canais baixados do TikTok. Visualize os vídeos nos mesmos cards com legendas originais do .info.txt e publique em suas contas.'),
    ),
    h('div', { style: 'display:flex; align-items:center; gap:10px;' },
      btnAtualizar,
    ),
  );

  // Barra de métricas rápidas
  const totalMb = creators.reduce((acc, c) => acc + (c.total_size_mb || 0), 0);
  const totalPublicados = creators.reduce((acc, c) => acc + (c.published_count || 0), 0);

  const statsBar = h('div', {
    style: 'display:flex; align-items:center; gap:16px; margin-bottom:24px; padding:12px 18px; background:var(--bg-card); border:1px solid var(--border-color); border-radius:var(--radius-md); flex-wrap:wrap;'
  },
    h('div', { style: 'display:flex; align-items:center; gap:8px;' },
      h('span', { style: 'font-weight:700; color:var(--text-main);' }, `👥 ${creators.length}`),
      h('span', { style: 'font-size:13px; color:var(--text-muted);' }, 'canais com downloads'),
    ),
    h('span', { style: 'color:var(--border-color);' }, '|'),
    h('div', { style: 'display:flex; align-items:center; gap:8px;' },
      h('span', { style: 'font-weight:700; color:var(--text-main);' }, `📹 ${totalVideos}`),
      h('span', { style: 'font-size:13px; color:var(--text-muted);' }, 'vídeos prontos'),
    ),
    h('span', { style: 'color:var(--border-color);' }, '|'),
    h('div', { style: 'display:flex; align-items:center; gap:8px;' },
      h('span', { style: 'font-weight:700; color:var(--text-main);' }, `💾 ${totalMb.toFixed(1)} MB`),
      h('span', { style: 'font-size:13px; color:var(--text-muted);' }, 'em disco'),
    ),
    h('span', { style: 'color:var(--border-color);' }, '|'),
    h('div', { style: 'display:flex; align-items:center; gap:8px;' },
      h('span', { style: 'font-weight:700; color:#22c55e;' }, `🚀 ${totalPublicados}`),
      h('span', { style: 'font-size:13px; color:var(--text-muted);' }, 'publicados no TikTok'),
    ),
  );

  // Grid de canais
  const grid = h('div', { class: 'sc-channel-grid' });

  if (creators.length === 0) {
    const empty = h('div', {
      class: 'sc-card',
      style: 'padding:48px 24px; text-align:center; color:var(--text-muted); grid-column:1 / -1;',
    },
      h('div', { style: 'font-size:36px; margin-bottom:12px;' }, '📥'),
      h('div', { style: 'font-size:16px; font-weight:600; color:var(--text-main); margin-bottom:6px;' }, 'Nenhum canal baixado ainda'),
      h('div', { style: 'font-size:13px; max-width:480px; margin:0 auto;' },
        'Os vídeos baixados pelo seu projeto em ',
        h('code', { style: 'background:var(--bg-subtle); padding:2px 6px; border-radius:4px;' }, 'tiktok-downloader-web/downloads'),
        ' aparecerão automaticamente aqui.'
      ),
    );
    grid.append(empty);
  } else {
    for (const c of creators) {
      const initial = (c.id || 'T').replace('@', '').charAt(0).toUpperCase();

      const card = h('div', { class: 'sc-channel-card' },
        // Cabeçalho do card
        h('div', { class: 'sc-channel-header' },
          h('div', { class: 'sc-tiktok-avatar' }, initial),
          h('div', { class: 'sc-channel-info' },
            h('div', { style: 'font-weight:700; font-size:16px; color:var(--text-main);' }, c.display_name || c.username),
            h('a', {
              href: c.url,
              target: '_blank',
              style: 'font-size:12px; color:var(--text-muted); text-decoration:none; display:inline-flex; align-items:center; gap:4px; margin-top:2px;',
            }, 'Perfil no TikTok ↗'),
          ),
          h('span', { class: 'sc-status-pill is-active' },
            h('span', { class: 'sc-status-dot' }),
            'Sincronizado'
          ),
        ),

        // Métricas do canal
        h('div', {
          style: 'display:grid; grid-template-columns:1fr 1fr; gap:8px; background:var(--bg-subtle); padding:12px; border-radius:var(--radius-md); font-size:12.5px;'
        },
          h('div', null,
            h('div', { style: 'color:var(--text-muted); font-size:11px;' }, 'VÍDEOS'),
            h('div', { style: 'font-weight:700; color:var(--text-main); font-size:14px;' }, `${c.video_count} vídeos`),
          ),
          h('div', null,
            h('div', { style: 'color:var(--text-muted); font-size:11px;' }, 'TAMANHO TOTAL'),
            h('div', { style: 'font-weight:700; color:var(--text-main); font-size:14px;' }, `${c.total_size_mb} MB`),
          ),
          h('div', null,
            h('div', { style: 'color:var(--text-muted); font-size:11px;' }, 'PUBLICADOS'),
            h('div', { style: 'font-weight:700; color:#22c55e;' }, `${c.published_count} vídeos`),
          ),
          h('div', null,
            h('div', { style: 'color:var(--text-muted); font-size:11px;' }, 'PENDENTES'),
            h('div', { style: 'font-weight:700; color:var(--text-main);' }, `${c.pending_count} vídeos`),
          ),
        ),

        // Bloco de Seleção de Conta TikTok Vinculada
        (() => {
          const contaAtual = obterContaCanalTikTok(c.folder);
          const selContaCanal = h('select', {
            class: 'sc-select sc-select-sm',
            style: 'width:100%; font-weight:600; cursor:pointer;',
            onchange: (e) => {
              const novaConta = e.target.value;
              c.target_account_id = novaConta;
              salvarContaCanalTikTok(c.folder, novaConta);
              const nome = (state.accounts.find((a) => a.id === novaConta) || {}).name || novaConta;
              toast(`Conta "${nome}" vinculada ao canal ${c.display_name || c.username}!`);
            },
          }, ...state.accounts.map((acc) =>
            h('option', {
              value: acc.id,
              selected: acc.id === contaAtual,
              text: `📱 ${acc.name} (${acc.id})`
            })
          ));

          return h('div', {
            class: 'sc-channel-account-box',
            style: 'margin:4px 0;'
          },
            h('div', { style: 'flex:1;' },
              h('div', { class: 'sc-channel-account-label', style: 'margin-bottom:6px; font-weight:700;' }, '📱 CONTA TIKTOK DE DESTINO:'),
              selContaCanal,
            ),
            h('span', {
              style: 'font-size:11px; font-weight:700; background:#ffffff; color:#15803d; border:1px solid rgba(34,197,94,0.4); padding:3px 8px; border-radius:6px; align-self:flex-start; margin-top:20px;',
            }, 'Exclusiva'),
          );
        })(),

        // Data de atualização
        h('div', { style: 'font-size:11.5px; color:var(--text-muted);' },
          `🕒 Último download: ${c.last_updated}`
        ),

        // Botões de ação do canal
        h('div', { style: 'display:flex; gap:10px; margin-top:auto;' },
          h('button', {
            class: 'sc-btn sc-btn-primary',
            style: 'flex:1; justify-content:center;',
            onclick: () => {
              const contaVinculada = obterContaCanalTikTok(c.folder);
              state.selectedTikTokCreator = c.folder;
              state.activeAccount = contaVinculada;
              state.selectedTikTokVideos.clear();
              renderTikTokDownloads(container);
            },
          }, `👁️ Ver Vídeos (${c.video_count})`),
          h('button', {
            class: 'sc-btn',
            style: 'justify-content:center;',
            title: 'Publicar todos os vídeos deste canal no TikTok',
            onclick: () => abrirModalPublicarLoteTikTok(c, container),
          }, '🚀 Publicar Todos'),
        ),
      );

      grid.append(card);
    }
  }

  container.append(head, statsBar, grid);
}

async function renderTikTokCreatorVideos(container, folder) {
  container.replaceChildren();

  const loadingWrap = h('div', { class: 'sc-card', style: 'padding:32px; text-align:center; color:var(--text-muted);' },
    `Carregando vídeos do canal ${folder}...`
  );
  container.append(loadingWrap);

  let videos = [];
  try {
    const data = await apiSupo(`/tiktok-downloads/creators/${encodeURIComponent(folder)}/videos`);
    videos = data.videos || [];
    state.tiktokVideosCache[folder] = videos;
  } catch (e) {
    toast('Erro ao buscar vídeos: ' + (e.message || e));
  }

  container.replaceChildren();

  const contaVinculada = obterContaCanalTikTok(folder);

  // Topo com Voltar
  const backBtn = h('button', {
    class: 'sc-back-link',
    onclick: () => {
      state.selectedTikTokCreator = null;
      state.selectedTikTokVideos.clear();
      renderTikTokDownloads(container);
    },
  }, '← Todos os Canais TikTok');

  const head = h('div', { class: 'sc-page-head', style: 'display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;' },
    h('div', null,
      backBtn,
      h('div', { style: 'display:flex; align-items:center; gap:10px; margin-top:6px;' },
        h('h1', { class: 'sc-page-title', style: 'margin:0;' }, `Canal ${folder}`),
        h('span', { class: 'sc-tiktok-badge' }, `${videos.length} vídeos prontos`),
      ),
      h('p', { class: 'sc-page-desc' }, 'Vídeos baixados e legendas extraídas do .info.txt. Selecione a conta de destino e clique em Publicar ou envie em lote.'),
    ),
  );

  // Barra de ferramentas em lote (Conta destino, Intervalo, Seleção)
  const chkSelecionarTodos = h('input', {
    type: 'checkbox',
    id: 'tt-select-all',
    style: 'cursor:pointer; width:16px; height:16px;',
    onchange: (e) => {
      const checked = e.target.checked;
      document.querySelectorAll('.tt-video-chk').forEach((cb) => {
        cb.checked = checked;
        const vid = cb.dataset.filename;
        if (checked) state.selectedTikTokVideos.add(vid);
        else state.selectedTikTokVideos.delete(vid);
      });
      atualizarContadorSelecao();
    },
  });

  const lblSelecao = h('label', {
    for: 'tt-select-all',
    style: 'font-size:13px; font-weight:600; cursor:pointer; display:flex; align-items:center; gap:6px;'
  }, chkSelecionarTodos, 'Selecionar Todos');

  const badgeSelecao = h('span', {
    class: 'sc-tag',
    style: 'font-weight:600;',
  }, `${state.selectedTikTokVideos.size} selecionados`);

  const selContaGlobal = h('select', {
    class: 'sc-select',
    style: 'width:auto; min-width:180px;',
    onchange: (e) => {
      state.activeAccount = e.target.value;
      salvarContaCanalTikTok(folder, e.target.value);
      atualizarSidebarStatus();
      // Atualiza também os selects individuais dos cards
      document.querySelectorAll('.card-tt-acc-select').forEach((sel) => {
        sel.value = e.target.value;
      });
      const nomeAcc = (state.accounts.find((a) => a.id === e.target.value) || {}).name || e.target.value;
      toast(`Conta "${nomeAcc}" definida para este canal!`);
    },
  }, ...state.accounts.map((acc) =>
    h('option', { value: acc.id, selected: acc.id === contaVinculada, text: `📱 Conta: ${acc.name}` })
  ));

  const inputIntervalo = h('input', {
    type: 'number',
    class: 'sc-input',
    value: '10',
    min: '1',
    max: '120',
    style: 'width:68px; text-align:center;',
  });

  const btnPublicarLote = h('button', {
    class: 'sc-btn sc-btn-primary',
    style: 'display:flex; align-items:center; gap:6px;',
    disabled: state.selectedTikTokVideos.size === 0,
    onclick: async () => {
      const selecionados = Array.from(state.selectedTikTokVideos);
      if (selecionados.length === 0) {
        toast('Selecione ao menos um vídeo para publicar.');
        return;
      }

      const itens = videos.filter((v) => state.selectedTikTokVideos.has(v.filename));
      const targetAcc = selContaGlobal.value || state.activeAccount;
      const accObj = state.accounts.find((a) => a.id === targetAcc) || { name: targetAcc };
      const intervalMin = parseInt(inputIntervalo.value, 10) || 10;

      const conf = confirm(
        `Deseja iniciar a publicação em lote de ${itens.length} vídeo(s) na conta "${accObj.name}" com intervalo de ${intervalMin} minutos?\n\nO progresso poderá ser acompanhado em tempo real no Publicador TikTok.`
      );
      if (!conf) return;

      btnPublicarLote.disabled = true;
      btnPublicarLote.textContent = '🚀 Enfileirando...';

      try {
        await apiSupo('/tiktok-downloads/publish-batch', {
          method: 'POST',
          body: JSON.stringify({
            items: itens,
            account_id: targetAcc,
            interval_min: intervalMin,
          }),
        });
        toast(`Fila iniciada com ${itens.length} vídeos na conta "${accObj.name}"!`);
        navegar('publicador');
      } catch (err) {
        toast('Erro ao iniciar fila: ' + err.message);
        btnPublicarLote.disabled = false;
        btnPublicarLote.textContent = `🚀 Publicar Selecionados (${state.selectedTikTokVideos.size})`;
      }
    },
  }, `🚀 Publicar Selecionados (${state.selectedTikTokVideos.size})`);

  function atualizarContadorSelecao() {
    badgeSelecao.textContent = `${state.selectedTikTokVideos.size} selecionados`;
    btnPublicarLote.disabled = state.selectedTikTokVideos.size === 0;
    btnPublicarLote.textContent = `🚀 Publicar Selecionados (${state.selectedTikTokVideos.size})`;
    chkSelecionarTodos.checked = state.selectedTikTokVideos.size > 0 && state.selectedTikTokVideos.size === videos.length;
  }

  const toolbar = h('div', {
    class: 'sc-card',
    style: 'display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:14px; padding:12px 18px; margin-bottom:24px;'
  },
    h('div', { style: 'display:flex; align-items:center; gap:12px;' },
      lblSelecao,
      badgeSelecao,
    ),
    h('div', { style: 'display:flex; align-items:center; gap:12px; flex-wrap:wrap;' },
      h('span', { style: 'font-size:12.5px; color:var(--text-muted); font-weight:600;' }, 'Publicar na:'),
      selContaGlobal,
      h('div', { style: 'display:flex; align-items:center; gap:6px;' },
        h('span', { style: 'font-size:12.5px; color:var(--text-muted);' }, 'Intervalo:'),
        inputIntervalo,
        h('span', { style: 'font-size:12.5px; color:var(--text-muted);' }, 'min'),
      ),
      btnPublicarLote,
    ),
  );

  // Grid de Vídeos nos mesmos cards do TikClip
  const grid = h('div', { class: 'sc-clip-grid' });

  if (videos.length === 0) {
    grid.append(h('div', {
      class: 'sc-card',
      style: 'padding:40px; text-align:center; color:var(--text-muted); grid-column:1 / -1;'
    }, 'Nenhum vídeo .mp4 encontrado nesta pasta.'));
  } else {
    for (const v of videos) {
      const isSelected = state.selectedTikTokVideos.has(v.filename);

      // Elemento de vídeo com suporte a Capa (Poster)
      const videoEl = h('video', {
        class: 'sc-clip-video',
        src: v.video_url,
        poster: v.cover_url || '',
        controls: true,
        preload: 'metadata',
        playsinline: true,
      });

      // Caixa de Mídia (aspect ratio 9:16 vertical)
      const media = h('div', { class: 'sc-clip-media' },
        videoEl,
        h('div', { class: 'sc-clip-badges' },
          h('span', { class: 'sc-clip-badge' }, `${v.size_mb} MB`),
          v.is_published
            ? h('span', { class: 'sc-clip-badge sc-badge-published' }, `✔ Publicado (${v.published_account || 'TikTok'})`)
            : h('span', { class: 'sc-clip-badge' }, 'Pronto'),
        ),
      );

      // Checkbox de seleção do card
      const chk = h('input', {
        type: 'checkbox',
        class: 'tt-video-chk',
        'data-filename': v.filename,
        checked: isSelected,
        style: 'cursor:pointer; width:16px; height:16px; margin-top:2px; flex-shrink:0;',
        onchange: (e) => {
          if (e.target.checked) state.selectedTikTokVideos.add(v.filename);
          else state.selectedTikTokVideos.delete(v.filename);
          atualizarContadorSelecao();
        },
      });

      // Textarea para legenda e descrição do .info.txt
      const txtDescricao = h('textarea', {
        class: 'sc-tiktok-caption-input',
        title: 'Legenda extraída do .info.txt (editável antes de publicar)',
      });
      txtDescricao.value = v.description || v.title;

      // Tags
      const tagsWrap = h('div', { class: 'sc-clip-tags', style: 'max-height:48px; overflow-y:auto; margin:6px 0;' },
        ...(v.hashtags || []).map((t) => h('span', { class: 'sc-tag' }, t))
      );

      // Select de conta do TikTok específico deste card
      const selContaCard = h('select', {
        class: 'sc-select sc-select-sm card-tt-acc-select',
        style: 'font-size:12px; width:100%;',
      }, ...state.accounts.map((acc) =>
        h('option', { value: acc.id, selected: acc.id === contaVinculada, text: `📱 Conta: ${acc.name}` })
      ));

      // Botão Publicar Avulso
      const btnPublicarIndividual = h('button', {
        class: 'sc-btn sc-btn-primary sc-btn-sm',
        style: 'margin-left:auto; display:flex; align-items:center; gap:4px;',
        onclick: async () => {
          const chosenAcc = selContaCard.value || state.activeAccount;
          const accObj = state.accounts.find((a) => a.id === chosenAcc) || { name: chosenAcc };

          const conf = confirm(`Publicar este vídeo agora no TikTok na conta "${accObj.name}"?`);
          if (!conf) return;

          btnPublicarIndividual.disabled = true;
          btnPublicarIndividual.textContent = '🚀 Enviando...';

          try {
            toast(`Enviando vídeo para o TikTok (${accObj.name})...`);
            await apiSupo('/tiktok-downloads/publish', {
              method: 'POST',
              body: JSON.stringify({
                folder: v.folder,
                filename: v.filename,
                account_id: chosenAcc,
                title: v.title,
                description: txtDescricao.value,
                tags: v.hashtags,
              }),
            });
            toast(`Vídeo publicado com sucesso na conta "${accObj.name}"!`);
            btnPublicarIndividual.textContent = '✔ Publicado';
            btnPublicarIndividual.classList.remove('sc-btn-primary');
            btnPublicarIndividual.style.background = '#22c55e';
            btnPublicarIndividual.style.borderColor = '#22c55e';
          } catch (err) {
            toast('Erro ao publicar: ' + err.message);
            btnPublicarIndividual.disabled = false;
            btnPublicarIndividual.textContent = '🚀 Publicar';
          }
        },
      }, '🚀 Publicar');

      // Botão Gerar / Ver Capa
      const btnGerarCapa = h('button', {
        class: 'sc-btn sc-btn-sm',
        title: 'Gerar capa personalizada do vídeo com FFmpeg',
        onclick: async () => {
          btnGerarCapa.disabled = true;
          btnGerarCapa.textContent = '🖼️ Gerando...';
          try {
            const res = await apiSupo('/tiktok-downloads/generate-cover', {
              method: 'POST',
              body: JSON.stringify({ folder: v.folder, filename: v.filename, second: 1.5 }),
            });
            videoEl.poster = res.cover_url;
            toast('Capa gerada com sucesso para o vídeo!');
            btnGerarCapa.textContent = '🖼️ Capa ✔';
          } catch (err) {
            toast('Erro ao gerar capa: ' + err.message);
            btnGerarCapa.disabled = false;
            btnGerarCapa.textContent = '🖼️ Criar Capa';
          }
        },
      }, v.cover_url ? '🖼️ Capa ✔' : '🖼️ Criar Capa');

      // Botão Copiar Legenda
      const btnCopiar = h('button', {
        class: 'sc-btn sc-btn-sm',
        title: 'Copiar legenda e hashtags para a área de transferência',
        onclick: () => {
          navigator.clipboard.writeText(txtDescricao.value);
          toast('Legenda copiada para a área de transferência!');
        },
      }, '📋 Copiar');

      // Link para download local
      const linkBaixar = h('a', {
        class: 'sc-btn sc-btn-sm',
        href: v.video_url,
        download: v.filename,
        style: 'text-decoration:none;',
        title: 'Baixar arquivo de vídeo .mp4',
      }, '📥 Baixar');

      // Corpo do Card
      const body = h('div', { class: 'sc-clip-body' },
        h('div', { style: 'display:flex; align-items:flex-start; gap:8px;' },
          chk,
          h('div', {
            class: 'sc-clip-title',
            style: 'font-size:13.5px; line-height:1.35; flex:1;',
            title: v.title,
          }, v.title),
        ),

        h('div', { style: 'display:flex; align-items:center; justify-content:space-between; font-size:11.5px; color:var(--text-muted); margin:4px 0;' },
          h('span', null, `📅 ${v.created_at || 'Recente'}`),
          h('a', {
            href: v.original_url,
            target: '_blank',
            style: 'color:var(--text-muted); text-decoration:none;',
          }, '🔗 TikTok ↗'),
        ),

        h('div', { style: 'margin-top:4px;' },
          h('div', { style: 'font-size:11px; font-weight:700; color:var(--text-muted); margin-bottom:2px;' }, '📝 LEGENDA (.info.txt):'),
          txtDescricao,
        ),

        tagsWrap,

        h('div', { style: 'margin-top:6px;' },
          h('div', { style: 'font-size:11px; font-weight:700; color:var(--text-muted); margin-bottom:2px;' }, 'CONTA DESTINO:'),
          selContaCard,
        ),

        h('div', { class: 'sc-clip-actions', style: 'margin-top:10px;' },
          btnGerarCapa,
          btnCopiar,
          linkBaixar,
          btnPublicarIndividual,
        ),
      );

      const card = h('div', { class: 'sc-clip-card' }, media, body);
      grid.append(card);
    }
  }

  container.append(head, toolbar, grid);
}

function abrirModalPublicarLoteTikTok(creator, parentContainer) {
  const backdrop = h('div', { class: 'sc-modal-backdrop' });

  const selConta = h('select', {
    class: 'sc-select',
    style: 'width:100%;',
  }, ...state.accounts.map((acc) =>
    h('option', { value: acc.id, selected: acc.id === state.activeAccount, text: `📱 ${acc.name} (${acc.id})` })
  ));

  const inputIntervalo = h('input', {
    type: 'number',
    class: 'sc-input',
    value: '10',
    min: '1',
    max: '120',
  });

  const chkApenasPendentes = h('input', {
    type: 'checkbox',
    id: 'chk-apenas-pendentes',
    checked: true,
    style: 'cursor:pointer; width:16px; height:16px;',
  });

  const btnConfirmar = h('button', {
    class: 'sc-btn sc-btn-primary',
    onclick: async () => {
      btnConfirmar.disabled = true;
      btnConfirmar.textContent = '🚀 Iniciando fila...';

      try {
        const vidsData = await apiSupo(`/tiktok-downloads/creators/${encodeURIComponent(creator.folder)}/videos`);
        let vids = vidsData.videos || [];
        if (chkApenasPendentes.checked) {
          vids = vids.filter((v) => !v.is_published);
        }

        if (vids.length === 0) {
          toast('Nenhum vídeo pendente para publicar neste canal.');
          backdrop.remove();
          return;
        }

        const accountId = selConta.value || state.activeAccount;
        const intervalMin = parseInt(inputIntervalo.value, 10) || 10;

        await apiSupo('/tiktok-downloads/publish-batch', {
          method: 'POST',
          body: JSON.stringify({
            items: vids,
            account_id: accountId,
            interval_min: intervalMin,
          }),
        });

        toast(`Fila iniciada com ${vids.length} vídeos no Publicador TikTok!`);
        backdrop.remove();
        navegar('publicador');
      } catch (err) {
        toast('Erro ao iniciar publicação: ' + err.message);
        btnConfirmar.disabled = false;
        btnConfirmar.textContent = 'Confirmar e Publicar';
      }
    },
  }, 'Confirmar e Publicar');

  const btnCancelar = h('button', {
    class: 'sc-btn',
    onclick: () => backdrop.remove(),
  }, 'Cancelar');

  const modal = h('div', { class: 'sc-modal-box', style: 'max-width:480px;' },
    h('div', { class: 'sc-modal-head' },
      h('h2', { class: 'sc-modal-title' }, `Publicar Vídeos de ${creator.display_name}`),
      h('button', { class: 'sc-btn-close', onclick: () => backdrop.remove() }, '✕'),
    ),
    h('div', { class: 'sc-modal-body', style: 'display:flex; flex-direction:column; gap:16px;' },
      h('p', { class: 'sc-page-desc' },
        `Configure a publicação automática dos vídeos baixados do canal ${creator.display_name} para a sua conta do TikTok.`
      ),
      h('div', { class: 'sc-form-group' },
        h('label', { class: 'sc-form-label' }, 'Conta TikTok de Destino'),
        selConta,
        h('div', { class: 'sc-form-hint' }, 'Os vídeos serão enviados exclusivamente para esta conta.'),
      ),
      h('div', { class: 'sc-form-group' },
        h('label', { class: 'sc-form-label' }, 'Intervalo entre postagens (minutos)'),
        inputIntervalo,
        h('div', { class: 'sc-form-hint' }, 'Recomendado: 10 a 20 minutos para proteger a reputação da conta.'),
      ),
      h('label', { style: 'display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px;' },
        chkApenasPendentes,
        `Publicar apenas vídeos que ainda não foram enviados (${creator.pending_count} vídeos)`,
      ),
    ),
    h('div', { class: 'sc-modal-foot' },
      btnCancelar,
      btnConfirmar,
    ),
  );

  backdrop.append(modal);
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) backdrop.remove();
  });
  document.body.append(backdrop);
}


/* ==========================================================================
   VIEW 5: ESTÚDIO IA (GERAR DO ZERO, DUBLAR, SPLIT, BAIXAR TIKTOK)
   ========================================================================== */
function renderEstudio(container) {
  const wrap = h('div', { class: 'sc-page-narrow' });

  const tabs = [
    ['gerar', '✨ Gerar do Zero (IA)'],
    ['dublar', '🎙️ Dublar Vídeo'],
    ['split', '🎮 Vídeo Split (Retenção)'],
    ['baixar', '⬇️ Baixar do TikTok'],
  ];

  const tabBar = h('div', { style: 'display:flex; gap:8px; margin-bottom:22px; flex-wrap:wrap;' });
  for (const [id, label] of tabs) {
    tabBar.append(
      h('button', {
        class: `sc-btn ${state.estudioTab === id ? 'sc-btn-primary' : ''}`,
        onclick: () => {
          state.estudioTab = id;
          renderEstudio(container);
        },
      }, label),
    );
  }

  const body = h('div');
  wrap.append(
    h('div', { class: 'sc-page-head' },
      h('h1', { class: 'sc-page-title', text: 'Estúdio IA — Ferramentas Criativas' }),
      h('p', { class: 'sc-page-desc', text: `Gere vídeos inéditos por assunto usando ${state.ai.active_provider === 'gemini' ? 'Google Gemini' : 'DeepSeek'}, duble vídeos ou monte telas divididas.` }),
    ),
    tabBar,
    body,
  );
  container.replaceChildren(wrap);

  if (state.estudioTab === 'gerar') {
    const inpAssunto = h('input', { class: 'sc-input', placeholder: 'Ex.: 5 mistérios do oceano profundo que a ciência não explica' });
    const selDur = h('select', { class: 'sc-select' },
      h('option', { value: '30', text: '30 segundos' }),
      h('option', { value: '45', selected: true, text: '45 segundos' }),
      h('option', { value: '60', text: '60 segundos' }),
    );
    const statusText = h('div', { style: 'margin-top:12px; font-size:13px; color:var(--text-secondary);' });
    const galeria = h('div', { class: 'sc-clips-grid', style: 'margin-top:20px;' });

    body.append(
      h('div', { class: 'sc-card' },
        h('div', { class: 'sc-card-title', style: 'margin-bottom:12px;', text: 'Gerar Vídeo Inédito a partir de um Assunto' }),
        h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Assunto ou Tema do Vídeo' }), inpAssunto),
        h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Duração Alvo' }), selDur),
        h('button', {
          class: 'sc-btn sc-btn-primary',
          onclick: async () => {
            if (!inpAssunto.value.trim()) { toast('Digite um assunto.'); return; }
            try {
              const r = await apiExt('/gerar/iniciar', {
                method: 'POST',
                body: JSON.stringify({ assunto: inpAssunto.value.trim(), duracao: Number(selDur.value) }),
              });
              toast(r.mensagem || 'Geração iniciada!');
            } catch (e) {
              toast('Erro: ' + e.message);
            }
          },
        }, `✨ Gerar com ${state.ai.active_provider === 'gemini' ? 'Google Gemini' : 'DeepSeek'}`),
        statusText,
      ),
      galeria,
    );

    async function carregarGerados() {
      try {
        const [st, lst] = await Promise.all([apiExt('/gerar/status'), apiExt('/gerar/lista')]);
        statusText.textContent = st.rodando ? `⏳ ${st.mensagem || 'Gerando vídeo...'}` : (st.mensagem || '');
        galeria.replaceChildren();
        for (const item of lst.itens || []) {
          galeria.append(
            h('div', { class: 'sc-clip-card' },
              h('div', { class: 'sc-clip-media' },
                h('video', { class: 'sc-clip-video', src: item.video, controls: true, preload: 'metadata' }),
              ),
              h('div', { class: 'sc-clip-body' },
                h('div', { class: 'sc-clip-title', text: item.titulo || item.assunto }),
                h('div', { class: 'sc-clip-actions' },
                  h('a', { class: 'sc-btn sc-btn-sm', href: item.video, download: `${item.id}.mp4`, style: 'text-decoration:none' }, '⬇️ Baixar'),
                  h('button', {
                    class: 'sc-btn sc-btn-primary sc-btn-sm',
                    style: 'margin-left:auto',
                    onclick: async () => {
                      await apiExt('/gerar/publicar', { method: 'POST', body: JSON.stringify({ id: item.id, conta: state.activeAccount }) });
                      toast('Publicando vídeo gerado no TikTok!');
                    },
                  }, 'Publicar'),
                ),
              ),
            ),
          );
        }
      } catch { /* ignore */ }
    }
    carregarGerados();
    state.estudioTimer = setInterval(carregarGerados, 4000);
  } else if (state.estudioTab === 'baixar') {
    const inpUrl = h('input', { class: 'sc-input', placeholder: 'https://www.tiktok.com/@usuario/video/...' });
    body.append(
      h('div', { class: 'sc-card' },
        h('div', { class: 'sc-card-title', style: 'margin-bottom:12px;', text: 'Baixar Vídeo do TikTok sem Marca d’Água' }),
        h('div', { class: 'sc-field' }, inpUrl),
        h('button', {
          class: 'sc-btn sc-btn-primary',
          onclick: async () => {
            if (!inpUrl.value.trim()) return;
            try {
              await apiExt('/baixar/tiktok', { method: 'POST', body: JSON.stringify({ url: inpUrl.value.trim() }) });
              toast('Download iniciado!');
            } catch (e) {
              toast('Erro: ' + e.message);
            }
          },
        }, '⬇️ Baixar Vídeo'),
      ),
    );
  } else {
    body.append(
      h('div', { class: 'sc-card' },
        h('p', { text: `Módulo "${state.estudioTab.toUpperCase()}" conectado ao motor local em %APPDATA%\\TikClip.` }),
      ),
    );
  }
}

/* ==========================================================================
   VIEW 6: AGENTES E API (IDÊNTICO AO PRINT 3 DO SUPOCLIP)
   ========================================================================== */
async function renderAgentes(container) {
  const wrap = h('div', { class: 'sc-page-narrow' });

  wrap.append(
    h('div', { class: 'sc-page-head' },
      h('h1', { class: 'sc-page-title', text: 'Agentes e API' }),
      h('p', { class: 'sc-page-desc' },
        'Deixe Claude, Cursor e outros agentes de IA cortarem vídeos por você com o ',
        h('b', null, 'servidor MCP do TikClip'),
        ', ou chame a API REST no seu código. Trate as chaves como senhas: elas dão acesso total à sua conta.',
      ),
    ),
  );

  // Dois cards superiores (Conectar agente MCP / Referência API REST)
  wrap.append(
    h('div', { class: 'sc-agent-cards' },
      h('div', {
        class: 'sc-agent-card',
        onclick: () => {
          const modal = document.getElementById('dialog-modal');
          const box = document.getElementById('dialog-modal-content');
          const mcpJson = JSON.stringify({
            mcpServers: {
              tikclip: {
                command: 'python',
                args: ['backend/mcp_server.py'],
              },
            },
          }, null, 2);
          modal.style.display = 'flex';
          box.replaceChildren(
            h('h3', { style: 'font-size:18px; font-weight:700; margin-bottom:10px;', text: 'Configuração do Servidor MCP (Claude / Cursor)' }),
            h('p', { style: 'font-size:13px; color:var(--text-secondary); margin-bottom:12px;', text: 'Adicione este bloco no seu arquivo mcp_config.json:' }),
            h('pre', { style: 'background:var(--bg-subtle); padding:14px; border-radius:8px; font-size:12px; overflow-x:auto;', text: mcpJson }),
            h('div', { style: 'display:flex; justify-content:flex-end; gap:8px; margin-top:16px;' },
              h('button', {
                class: 'sc-btn sc-btn-primary',
                onclick: () => { navigator.clipboard.writeText(mcpJson); toast('Configuração MCP copiada!'); },
              }, 'Copiar JSON'),
              h('button', { class: 'sc-btn', onclick: () => { modal.style.display = 'none'; } }, 'Fechar'),
            ),
          );
        },
      },
        h('div', { class: 'sc-card-icon' }, '🤖'),
        h('div', null,
          h('div', { class: 'sc-card-title', text: 'Conectar um agente ↗' }),
          h('div', { class: 'sc-card-sub', text: 'Configure o servidor MCP no Claude, no Cursor e em outros clientes MCP.' }),
        ),
      ),
      h('div', {
        class: 'sc-agent-card',
        onclick: () => window.open('/docs', '_blank'),
      },
        h('div', { class: 'sc-card-icon' }, '{ }'),
        h('div', null,
          h('div', { class: 'sc-card-title', text: 'Referência da API REST ↗' }),
          h('div', { class: 'sc-card-sub', text: 'Endpoints, autenticação e documentação interativa OpenAPI para acessar a API direto.' }),
        ),
      ),
    ),
  );

  // Criar nova chave
  const inpKeyName = h('input', { class: 'sc-input', placeholder: 'ex.: MCP do meu notebook' });
  const keysList = h('div', { style: 'margin-top:24px;' });

  wrap.append(
    h('div', { class: 'sc-card' },
      h('div', { class: 'sc-card-title', style: 'margin-bottom:10px;', text: 'Criar uma nova chave' }),
      h('div', { style: 'display:flex; gap:10px;' },
        inpKeyName,
        h('button', {
          class: 'sc-btn sc-btn-primary',
          style: 'flex-shrink:0',
          onclick: async () => {
            const res = await apiSupo('/api-keys', {
              method: 'POST',
              body: JSON.stringify({ name: inpKeyName.value.trim() || 'Chave Local' }),
            });
            inpKeyName.value = '';
            toast('Chave criada com sucesso!');
            renderKeys(res.items || []);
          },
        }, '+ Criar'),
      ),
    ),
    keysList,
  );

  function renderKeys(items) {
    keysList.replaceChildren(
      h('div', { style: 'font-size:11.5px; font-weight:700; color:var(--text-muted); letter-spacing:0.05em; margin-bottom:10px;', text: 'SUAS CHAVES' }),
    );
    if (!items.length) {
      keysList.append(h('p', { style: 'font-size:13px; color:var(--text-muted)', text: 'Ainda não há chaves de API.' }));
      return;
    }
    for (const k of items) {
      keysList.append(
        h('div', { class: 'sc-card', style: 'padding:14px 18px; display:flex; align-items:center; justify-content:space-between;' },
          h('div', null,
            h('div', { style: 'font-weight:600; font-size:13.5px;', text: k.name }),
            h('div', { style: 'font-size:12px; color:var(--text-muted); font-family:monospace;', text: `${k.masked} · Criada em ${k.created_at}` }),
          ),
          h('div', { style: 'display:flex; gap:8px;' },
            h('button', {
              class: 'sc-btn sc-btn-sm',
              onclick: () => { navigator.clipboard.writeText(k.token); toast('Chave copiada!'); },
            }, 'Copiar'),
            h('button', {
              class: 'sc-btn sc-btn-sm',
              style: 'color:var(--err-color)',
              onclick: async () => {
                const r = await apiSupo(`/api-keys/${k.id}`, { method: 'DELETE' });
                renderKeys(r.items || []);
              },
            }, 'Revogar'),
          ),
        ),
      );
    }
  }

  container.replaceChildren(wrap);
  const res = await apiSupo('/api-keys').catch(() => ({ items: [] }));
  renderKeys(res.items || []);
}

/* ==========================================================================
   VIEW 7: CONFIGURAÇÕES (PRINT 4 DO SUPOCLIP + CHAVE GEMINI vs DEEPSEEK)
   ========================================================================== */
async function renderConfiguracoes(container) {
  await carregarEstadoInicial();

  const wrap = h('div', { class: 'sc-page-narrow' });

  wrap.append(
    h('div', { class: 'sc-page-head' },
      h('h1', { class: 'sc-page-title', text: 'Configurações' }),
      h('p', { class: 'sc-page-desc', text: 'Padrões para novas gerações, seleção de API de IA (Google Gemini ou DeepSeek), legendas e conexões.' }),
    ),
  );

  /* ------------------------------------------------------------------------
     CARD 1: MOTOR DE IA — CHAVE SELETORA GOOGLE GEMINI vs DEEPSEEK
     ------------------------------------------------------------------------ */
  let provSelecionado = state.ai.active_provider || 'deepseek';

  const inpGemKey = h('input', {
    class: 'sc-input',
    type: 'password',
    placeholder: state.ai.gemini.has_key
      ? `Chave salva (${state.ai.gemini.masked_key}) — digite para substituir`
      : 'Cole sua chave da API do Google Gemini (AIza...)',
  });

  const selGemModel = h('select', { class: 'sc-select' },
    ...(state.ai.gemini.models.length ? state.ai.gemini.models : [
      { id: 'gemini-3.5-flash', label: 'Gemini 3.5 Flash (Recomendado · Rápido e Preciso)' },
      { id: 'gemini-3.5-flash-lite', label: 'Gemini 3.5 Flash Lite (Ultra Rápido)' },
      { id: 'gemini-3.8-flash', label: 'Gemini 3.8 Flash (Nova Geração)' },
      { id: 'gemini-flash-latest', label: 'Gemini Flash Latest (Última Versão Automática)' },
    ]).map((m) => h('option', { value: m.id, selected: m.id === state.ai.gemini.model, text: m.label })),
  );

  const inpDsKey = h('input', {
    class: 'sc-input',
    type: 'password',
    placeholder: state.ai.deepseek.has_key
      ? `Chave salva (${state.ai.deepseek.masked_key}) — digite para substituir`
      : 'Cole sua chave da API do DeepSeek (sk-...)',
  });

  const selDsModel = h('select', { class: 'sc-select' },
    ...(state.ai.deepseek.models.length ? state.ai.deepseek.models : [
      { id: 'deepseek-chat', label: 'DeepSeek V3 Chat (Recomendado)' },
      { id: 'deepseek-reasoner', label: 'DeepSeek R1 Reasoner' },
    ]).map((m) => h('option', { value: m.id, selected: m.id === state.ai.deepseek.model, text: m.label })),
  );

  const testResultMsg = h('div', { style: 'margin-top:12px; font-size:13px; font-weight:600;' });

  const optGemini = h('button', {
    type: 'button',
    class: `sc-ai-option ${provSelecionado === 'gemini' ? 'is-selected' : ''}`,
    onclick: () => selecionarProvedorUI('gemini'),
  });

  const optDeepseek = h('button', {
    type: 'button',
    class: `sc-ai-option ${provSelecionado === 'deepseek' ? 'is-selected' : ''}`,
    onclick: () => selecionarProvedorUI('deepseek'),
  });

  const boxGemini = h('div', { class: 'sc-ai-provider-box' });
  const boxDeepseek = h('div', { class: 'sc-ai-provider-box' });

  function atualizarCardsSwitch() {
    const isGem = provSelecionado === 'gemini';
    optGemini.className = `sc-ai-option ${isGem ? 'is-selected' : ''}`;
    optGemini.replaceChildren(
      h('div', { class: 'sc-ai-option-top' },
        h('span', { class: 'sc-ai-name', text: '✨ Google Gemini API' }),
        isGem ? h('span', { class: 'sc-ai-pill-active', text: 'EM USO' }) : null,
      ),
      h('span', { style: 'font-size:12px; color:var(--text-secondary)', text: 'Rápido, multimodal e excelente para cortes virais em português.' }),
    );

    optDeepseek.className = `sc-ai-option ${!isGem ? 'is-selected' : ''}`;
    optDeepseek.replaceChildren(
      h('div', { class: 'sc-ai-option-top' },
        h('span', { class: 'sc-ai-name', text: '🧠 DeepSeek API' }),
        !isGem ? h('span', { class: 'sc-ai-pill-active', text: 'EM USO' }) : null,
      ),
      h('span', { style: 'font-size:12px; color:var(--text-secondary)', text: 'Alta precisão na análise de transcrições e geração de roteiros.' }),
    );

    boxGemini.className = `sc-ai-provider-box ${isGem ? 'is-active-box' : ''}`;
    boxDeepseek.className = `sc-ai-provider-box ${!isGem ? 'is-active-box' : ''}`;
  }

  async function selecionarProvedorUI(novo) {
    provSelecionado = novo;
    atualizarCardsSwitch();
    try {
      const res = await apiSupo('/ai/switch', {
        method: 'POST',
        body: JSON.stringify({ provider: novo }),
      });
      state.ai = res;
      atualizarSidebarStatus();
      toast(`API ativa alterada para ${novo === 'gemini' ? 'Google Gemini' : 'DeepSeek'}!`);
    } catch (e) {
      toast('Erro ao trocar provedor: ' + e.message);
    }
  }

  atualizarCardsSwitch();

  boxGemini.append(
    h('div', { style: 'display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;' },
      h('strong', { text: 'Credenciais do Google Gemini' }),
      h('span', { style: 'font-size:12px; color:var(--text-muted)', text: state.ai.gemini.has_key ? `Salva: ${state.ai.gemini.masked_key}` : 'Nenhuma chave salva' }),
    ),
    h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Chave da API (GEMINI_API_KEY)' }), inpGemKey),
    h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Modelo do Gemini' }), selGemModel),
    h('button', {
      type: 'button',
      class: 'sc-btn sc-btn-sm',
      onclick: async () => {
        testResultMsg.style.color = 'var(--text-secondary)';
        testResultMsg.textContent = '⏳ Testando conexão com Google Gemini...';
        const r = await apiSupo('/ai/test', {
          method: 'POST',
          body: JSON.stringify({
            provider: 'gemini',
            api_key: inpGemKey.value.trim() || undefined,
            model: selGemModel.value,
          }),
        });
        if (r.ok) {
          testResultMsg.style.color = 'var(--ok-color)';
          testResultMsg.textContent = `✅ Gemini respondeu em ${r.latencia_ms}ms (${r.modelo}): "${r.resposta}"`;
        } else {
          testResultMsg.style.color = 'var(--err-color)';
          testResultMsg.textContent = `❌ Falha no Gemini: ${r.detalhe}`;
        }
      },
    }, '⚡ Testar API do Gemini'),
  );

  boxDeepseek.append(
    h('div', { style: 'display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;' },
      h('strong', { text: 'Credenciais do DeepSeek' }),
      h('span', { style: 'font-size:12px; color:var(--text-muted)', text: state.ai.deepseek.has_key ? `Salva: ${state.ai.deepseek.masked_key}` : 'Nenhuma chave salva' }),
    ),
    h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Chave da API (DEEPSEEK_API_KEY)' }), inpDsKey),
    h('div', { class: 'sc-field' }, h('label', { class: 'sc-label', text: 'Modelo do DeepSeek' }), selDsModel),
    h('button', {
      type: 'button',
      class: 'sc-btn sc-btn-sm',
      onclick: async () => {
        testResultMsg.style.color = 'var(--text-secondary)';
        testResultMsg.textContent = '⏳ Testando conexão com DeepSeek...';
        const r = await apiSupo('/ai/test', {
          method: 'POST',
          body: JSON.stringify({
            provider: 'deepseek',
            api_key: inpDsKey.value.trim() || undefined,
            model: selDsModel.value,
          }),
        });
        if (r.ok) {
          testResultMsg.style.color = 'var(--ok-color)';
          testResultMsg.textContent = `✅ DeepSeek respondeu em ${r.latencia_ms}ms (${r.modelo}): "${r.resposta}"`;
        } else {
          testResultMsg.style.color = 'var(--err-color)';
          testResultMsg.textContent = `❌ Falha no DeepSeek: ${r.detalhe}`;
        }
      },
    }, '⚡ Testar API do DeepSeek'),
  );

  const cardAi = h('div', { class: 'sc-card' },
    h('div', { class: 'sc-card-head' },
      h('div', { class: 'sc-card-icon' }, '🤖'),
      h('div', null,
        h('div', { class: 'sc-card-title', text: 'Motor de Inteligência Artificial (Chave Seletora Gemini / DeepSeek)' }),
        h('div', { class: 'sc-card-sub', text: 'Selecione abaixo qual API você quer usar para a geração de cortes e roteiros.' }),
      ),
    ),
    h('div', { class: 'sc-ai-switch-grid' }, optGemini, optDeepseek),
    boxGemini,
    boxDeepseek,
    h('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-top:16px;' },
      testResultMsg,
      h('button', {
        class: 'sc-btn sc-btn-primary',
        onclick: async () => {
          try {
            const novoEstado = await apiSupo('/ai', {
              method: 'POST',
              body: JSON.stringify({
                active_provider: provSelecionado,
                gemini_api_key: inpGemKey.value.trim(),
                gemini_model: selGemModel.value,
                deepseek_api_key: inpDsKey.value.trim(),
                deepseek_model: selDsModel.value,
              }),
            });
            state.ai = novoEstado;
            atualizarSidebarStatus();
            toast('Configurações de IA salvas com sucesso!');
            renderConfiguracoes(container);
          } catch (e) {
            toast('Erro ao salvar: ' + e.message);
          }
        },
      }, '💾 Salvar Chaves e Seleção de IA'),
    ),
  );

  /* ------------------------------------------------------------------------
     CARD 2: IDIOMA (IGUAL AO PRINT 4 DO SUPOCLIP)
     ------------------------------------------------------------------------ */
  const cardIdioma = h('div', { class: 'sc-card' },
    h('div', { class: 'sc-card-head' },
      h('div', { class: 'sc-card-icon' }, '文A'),
      h('div', null,
        h('div', { class: 'sc-card-title', text: 'Idioma' }),
        h('div', { class: 'sc-card-sub', text: 'O idioma usado no TikClip Web e nas legendas geradas.' }),
      ),
    ),
    h('select', { class: 'sc-select', style: 'max-width: 280px;' },
      h('option', { value: 'pt-BR', selected: true, text: 'Português' }),
    ),
  );

  /* ------------------------------------------------------------------------
     CARD 3: LEGENDAS PADRÃO COM PREVIEW VERTICAL EM TEMPO REAL (PRINT 4)
     ------------------------------------------------------------------------ */
  const subCfg = state.supoSettings.subtitle || { font_family: 'Tiktoksans Regular', font_size: 24, text_color: '#FFFFFF' };

  const previewText = h('div', {
    class: 'sc-phone-caption',
    text: 'Sua legenda vai ficar assim',
  });

  function atualizarPreviewLegenda() {
    previewText.style.fontFamily = subCfg.font_family.includes('Montserrat')
      ? "'Montserrat', sans-serif"
      : "'Inter', sans-serif";
    previewText.style.fontSize = `${Math.round(subCfg.font_size * 0.68)}px`;
    previewText.style.color = subCfg.text_color || '#FFFFFF';
  }
  atualizarPreviewLegenda();

  const selFonte = h('select', {
    class: 'sc-select',
    onchange: (e) => {
      subCfg.font_family = e.target.value;
      atualizarPreviewLegenda();
      salvarSubCfg();
    },
  },
    ...['Tiktoksans Regular', 'Montserrat Bold', 'Inter ExtraBold', 'Impact'].map((f) =>
      h('option', { value: f, selected: subCfg.font_family === f, text: `T  ${f}` }),
    ),
  );

  const labelPx = h('span', { style: 'font-size:12.5px; color:var(--text-muted);', text: `${subCfg.font_size}px` });
  const sliderSize = h('input', {
    class: 'sc-slider',
    type: 'range',
    min: '16',
    max: '40',
    value: String(subCfg.font_size || 24),
    oninput: (e) => {
      subCfg.font_size = Number(e.target.value);
      labelPx.textContent = `${subCfg.font_size}px`;
      atualizarPreviewLegenda();
    },
    onchange: () => salvarSubCfg(),
  });

  const inpHex = h('input', {
    class: 'sc-input',
    style: 'width: 100px; text-transform: uppercase; font-family: monospace;',
    value: subCfg.text_color || '#FFFFFF',
    oninput: (e) => {
      subCfg.text_color = e.target.value;
      atualizarPreviewLegenda();
    },
    onchange: () => salvarSubCfg(),
  });

  const cores = ['#FFFFFF', '#000000', '#FFD600', '#F87171', '#4ECDC4', '#45B7D1'];
  const colorRow = h('div', { class: 'sc-color-row' });

  function renderBolinhasCor() {
    colorRow.replaceChildren();
    for (const cor of cores) {
      colorRow.append(
        h('button', {
          type: 'button',
          class: `sc-color-dot ${(subCfg.text_color || '').toUpperCase() === cor ? 'is-selected' : ''}`,
          style: `background: ${cor}`,
          onclick: () => {
            subCfg.text_color = cor;
            inpHex.value = cor;
            atualizarPreviewLegenda();
            renderBolinhasCor();
            salvarSubCfg();
          },
        }),
      );
    }
    const picker = h('input', {
      type: 'color',
      value: subCfg.text_color || '#FFFFFF',
      style: 'width:28px; height:28px; border:none; border-radius:50%; cursor:pointer; background:transparent;',
      oninput: (e) => {
        subCfg.text_color = e.target.value.toUpperCase();
        inpHex.value = subCfg.text_color;
        atualizarPreviewLegenda();
        salvarSubCfg();
      },
    });
    colorRow.append(picker, inpHex);
  }
  renderBolinhasCor();

  async function salvarSubCfg() {
    try {
      await apiSupo('/settings', {
        method: 'POST',
        body: JSON.stringify({ subtitle: subCfg }),
      });
    } catch { /* ignore */ }
  }

  const cardLegendas = h('div', { class: 'sc-card' },
    h('div', { class: 'sc-card-head' },
      h('div', { class: 'sc-card-icon' }, 'T'),
      h('div', null,
        h('div', { class: 'sc-card-title', text: 'Legendas padrão' }),
        h('div', { class: 'sc-card-sub', text: 'Vale para cada geração nova. Você ainda pode mudar em cada vídeo.' }),
      ),
    ),
    h('div', { class: 'sc-sub-layout' },
      h('div', null,
        h('div', { class: 'sc-field' },
          h('label', { class: 'sc-label', text: 'Fonte' }),
          selFonte,
        ),
        h('div', { class: 'sc-field' },
          h('div', { style: 'display:flex; justify-content:space-between; margin-bottom:6px;' },
            h('label', { class: 'sc-label', style: 'margin:0', text: 'Tamanho' }),
            labelPx,
          ),
          h('div', { class: 'sc-slider-wrap' }, sliderSize),
        ),
        h('div', { class: 'sc-field', style: 'margin-bottom:0' },
          h('label', { class: 'sc-label', text: 'Cor' }),
          colorRow,
        ),
      ),
      h('div', { class: 'sc-phone-preview' }, previewText),
    ),
  );

  /* ------------------------------------------------------------------------
     CARD 4: CONEXÃO YOUTUBE (COOKIES) & NOTIFICAÇÕES
     ------------------------------------------------------------------------ */
  const ytInfo = h('div', { style: 'font-size:13px; color:var(--text-secondary); margin-bottom:12px;', text: 'Verificando cookies do YouTube...' });
  apiExt('/youtube/cookies').then((c) => {
    ytInfo.textContent = c.existe
      ? `✅ Arquivo cookies.txt ativo (${c.linhas || 0} entradas · atualizado há ${c.idade_horas || 0}h).`
      : '⚠️ Nenhum arquivo cookies.txt importado ainda.';
  }).catch(() => { ytInfo.textContent = 'Status de cookies disponível.'; });

  const fileCookies = h('input', {
    type: 'file',
    accept: '.txt',
    style: 'display:none',
    onchange: async (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      try {
        const conteudo = await f.text();
        await apiExt('/youtube/cookies/importar', {
          method: 'POST',
          body: JSON.stringify({ conteudo }),
        });
        toast('Cookies do YouTube importados com sucesso!');
        renderConfiguracoes(container);
      } catch (err) {
        toast('Erro ao importar cookies: ' + err.message);
      }
    },
  });

  const cardYoutube = h('div', { class: 'sc-card' },
    h('div', { class: 'sc-card-head' },
      h('div', { class: 'sc-card-icon' }, '▶️'),
      h('div', null,
        h('div', { class: 'sc-card-title', text: 'Autenticação e Cookies do YouTube' }),
        h('div', { class: 'sc-card-sub', text: 'Evita bloqueios de anti-bot ao baixar vídeos longos do YouTube.' }),
      ),
    ),
    ytInfo,
    fileCookies,
    h('div', { style: 'display:flex; gap:10px; flex-wrap:wrap;' },
      h('button', { class: 'sc-btn sc-btn-primary', onclick: () => fileCookies.click() }, '📂 Importar cookies.txt'),
      h('button', {
        class: 'sc-btn',
        onclick: async () => {
          try {
            await apiExt('/youtube/login/iniciar', { method: 'POST', body: JSON.stringify({}) });
            toast('Navegador de login do YouTube aberto!');
          } catch (e) {
            toast('Aviso: ' + e.message);
          }
        },
      }, '🌐 Fazer Login pelo Navegador'),
    ),
  );

  wrap.append(cardAi, cardIdioma, cardLegendas, cardYoutube);
  container.replaceChildren(wrap);
}

/* ==========================================================================
   MODAL DE BUSCA RÁPIDA (CTRL+K)
   ========================================================================== */
function configurarBuscaRapida() {
  const modal = document.getElementById('search-modal');
  const input = document.getElementById('search-modal-input');
  const results = document.getElementById('search-modal-results');

  function abrir() {
    modal.style.display = 'flex';
    input.value = '';
    atualizarResultados('');
    setTimeout(() => input.focus(), 30);
  }

  function fechar() {
    modal.style.display = 'none';
  }

  function atualizarResultados(termo) {
    const q = termo.toLowerCase().trim();
    results.replaceChildren();

    const comandos = [
      { label: '✨ Criar nova geração', acao: () => navegar('criar') },
      { label: '📚 Abrir Biblioteca de Clipes', acao: () => navegar('biblioteca') },
      { label: '🚀 Abrir Publicador TikTok em Lote', acao: () => navegar('publicador') },
      { label: '⚙️ Alternar IA (Google Gemini / DeepSeek) em Configurações', acao: () => navegar('configuracoes') },
    ];

    for (const cmd of comandos) {
      if (!q || cmd.label.toLowerCase().includes(q)) {
        results.append(
          h('button', {
            class: 'sc-search-item',
            onclick: () => { fechar(); cmd.acao(); },
          },
            h('span', { style: 'font-weight:500', text: cmd.label }),
            h('span', { style: 'font-size:11.5px; color:var(--text-muted)', text: 'Comando' }),
          ),
        );
      }
    }

    for (const p of state.projects) {
      const nome = p.title || p.name || '';
      if (!q || nome.toLowerCase().includes(q)) {
        results.append(
          h('button', {
            class: 'sc-search-item',
            onclick: () => { fechar(); navegar('projeto', p.id); },
          },
            h('span', { text: `🎬 ${nome}` }),
            h('span', { style: 'font-size:11.5px; color:var(--text-muted)', text: `${p.clips_ready} clipes` }),
          ),
        );
      }
    }
  }

  document.getElementById('btn-open-search').addEventListener('click', abrir);
  document.getElementById('btn-close-search').addEventListener('click', fechar);
  modal.addEventListener('click', (e) => { if (e.target === modal) fechar(); });
  input.addEventListener('input', (e) => atualizarResultados(e.target.value));

  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      abrir();
    } else if (e.key === 'Escape') {
      fechar();
      document.getElementById('dialog-modal').style.display = 'none';
    } else if (e.key.toLowerCase() === 'n' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
      navegar('criar');
    }
  });
}

/* ==========================================================================
   INICIALIZAÇÃO
   ========================================================================== */
window.addEventListener('DOMContentLoaded', async () => {
  const temaSalvo = localStorage.getItem('sc-theme') || 'light';
  aplicarTema(temaSalvo);

  document.getElementById('btn-theme-toggle').addEventListener('click', () => {
    const atual = document.documentElement.getAttribute('data-theme');
    aplicarTema(atual === 'dark' ? 'light' : 'dark');
  });

  document.getElementById('btn-new-gen').addEventListener('click', () => navegar('criar'));

  document.querySelectorAll('#sidebar-nav .sc-nav-item').forEach((btn) => {
    btn.addEventListener('click', () => navegar(btn.dataset.view));
  });

  document.getElementById('sidebar-ai-pill').addEventListener('click', () => navegar('configuracoes'));
  document.getElementById('sidebar-tt-pill').addEventListener('click', () => navegar('publicador'));

  configurarBuscaRapida();
  await carregarEstadoInicial();
  navegar('criar');
});
