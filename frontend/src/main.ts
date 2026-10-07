import './style.css';
import { mountChat } from './chat';

type Status = {
  mode: 'agent' | 'editing' | 'transition' | 'error';
  sync: { state: string; message: string; last_synced_at: string | null };
  backup: { last_success_at: string | null };
  phase2: { ready: boolean; reason: string };
  editor_available: boolean;
};

type ApiError = { detail?: string };
const app = document.querySelector<HTMLDivElement>('#app')!;
let csrfToken = '';
let currentStatus: Status | null = null;
let inFlight = false;
let pollTimer: number | undefined;
let authGeneration = 0;
let activeViewCleanup: (() => void) | undefined;

function setAuthenticated(token: string): void {
  authGeneration += 1;
  csrfToken = token;
  currentStatus = null;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = ''): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function formatDate(value: string | null): string {
  if (!value) return 'Ainda não registrado';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Data indisponível';
  return new Intl.DateTimeFormat('pt-BR', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'America/Sao_Paulo' }).format(date);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const requestGeneration = authGeneration;
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (init.method && init.method !== 'GET') headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' });
  if (response.status === 401 && path !== '/api/login' && requestGeneration === authGeneration) {
    csrfToken = '';
    showLogin('Sua sessão terminou. Entre novamente para continuar.');
    throw new Error('Sessão encerrada.');
  }
  if (requestGeneration !== authGeneration) throw new Error('Resposta de uma sessão anterior.');
  if (!response.ok) {
    let message = `Não foi possível concluir a ação (${response.status}).`;
    try {
      const body = await response.json() as ApiError;
      if (typeof body.detail === 'string' && body.detail.trim()) message = body.detail;
    } catch { /* Keep safe fallback for non-JSON responses. */ }
    throw new Error(message);
  }
  return response.status === 204 ? undefined as T : await response.json() as T;
}

function clearApp(): void {
  window.clearTimeout(pollTimer);
  activeViewCleanup?.();
  activeViewCleanup = undefined;
  app.replaceChildren();
}

function setAlert(host: HTMLElement, message: string): void {
  let alert = host.querySelector<HTMLElement>('[role="alert"]');
  if (!alert) {
    alert = el('p', 'alert');
    alert.setAttribute('role', 'alert');
    host.prepend(alert);
  }
  alert.textContent = message;
}

function showLogin(note = ''): void {
  authGeneration += 1;
  csrfToken = '';
  currentStatus = null;
  clearApp();
  const shell = el('main', 'auth-shell');
  const card = el('section', 'auth-card');
  card.setAttribute('aria-labelledby', 'login-title');
  const brand = el('div', 'brand-mark', 'MB');
  brand.setAttribute('aria-hidden', 'true');
  card.append(brand, el('p', 'eyebrow', 'SEU ESPAÇO PRIVADO'));
  const title = el('h1', '', 'MWSecondBrain');
  title.id = 'login-title';
  card.append(title, el('p', 'muted', 'Entre para acessar seu painel pessoal.'));
  if (note) setAlert(card, note);
  const form = el('form', 'login-form');
  const label = el('label', '', 'Senha');
  label.htmlFor = 'password';
  const password = el('input');
  password.id = 'password';
  password.name = 'password';
  password.type = 'password';
  password.autocomplete = 'current-password';
  password.required = true;
  password.autofocus = true;
  const submit = el('button', 'button button-primary', 'Entrar');
  submit.type = 'submit';
  form.append(label, password, submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    submit.disabled = true;
    submit.textContent = 'Verificando…';
    try {
      await request('/api/login', { method: 'POST', body: JSON.stringify({ password: password.value }) });
      const session = await request<{ authenticated: boolean; csrf_token: string }>('/api/session');
      setAuthenticated(session.csrf_token);
      await route();
    } catch (error) {
      if (error instanceof Error && error.message !== 'Sessão encerrada.') setAlert(card, error.message);
      password.value = '';
      submit.disabled = false;
      submit.textContent = 'Entrar';
      password.focus();
    }
  });
  card.append(form, el('p', 'security-note', 'Acesso protegido por sessão segura.'));
  shell.append(card);
  app.append(shell);
}

function pill(mode: Status['mode']): HTMLElement {
  const names: Record<Status['mode'], string> = { agent: 'Modo agente', editing: 'Modo edição', transition: 'Em transição', error: 'Atenção necessária' };
  const node = el('span', `status-pill status-${mode}`, names[mode]);
  return node;
}

function addMetric(label: string, value: string, icon: string): HTMLElement {
  const card = el('article', 'metric-card');
  const iconNode = el('span', 'metric-icon', icon);
  iconNode.setAttribute('aria-hidden', 'true');
  card.append(iconNode, el('p', 'metric-label', label), el('p', 'metric-value', value));
  return card;
}

function renderDashboard(status: Status, alertText = ''): void {
  currentStatus = status;
  clearApp();
  const page = el('main', 'page-shell');
  const top = el('header', 'topbar');
  const logo = el('a', 'wordmark', 'MW');
  logo.href = '/';
  logo.setAttribute('aria-label', 'MWSecondBrain, início');
  const accent = el('span', 'wordmark-accent');
  const rest = el('span', '', 'SecondBrain');
  logo.append(accent, rest);
  top.append(logo);
  const topActions = el('div', 'top-actions');
  const mode = pill(status.mode);
  topActions.append(mode);
  const logout = el('button', 'button button-quiet button-small', 'Sair');
  logout.type = 'button';
  logout.disabled = inFlight;
  logout.addEventListener('click', () => runAction(logout, async () => {
    await request('/api/logout', { method: 'POST', body: '{}' });
    showLogin('Você saiu da sua conta.');
  }));
  topActions.append(logout);
  top.append(topActions);

  const intro = el('section', 'intro');
  intro.append(el('p', 'eyebrow', 'PAINEL PESSOAL'));
  intro.append(el('h1', '', 'Seu espaço, em equilíbrio.'));
  intro.append(el('p', 'muted intro-copy', 'Acompanhe a sincronização do seu vault e escolha como deseja trabalhar.'));

  const metrics = el('section', 'metrics-grid');
  metrics.setAttribute('aria-label', 'Resumo do estado');
  metrics.append(addMetric('Última sincronização confirmada', formatDate(status.sync.last_synced_at), '↻'));
  metrics.append(addMetric('Último backup', formatDate(status.backup.last_success_at), '▣'));

  const statusCard = el('section', 'panel mode-panel');
  const panelCopy = el('div', 'panel-copy');
  panelCopy.append(el('p', 'eyebrow', 'CONTROLE DO VAULT'));
  const modeTitle: Record<Status['mode'], string> = {
    agent: 'O agente está pronto', editing: 'Você está editando', transition: 'Mudança de modo em andamento', error: 'O modo precisa de recuperação',
  };
  const modeDescription: Record<Status['mode'], string> = {
    agent: 'Sincronização e backup ficam disponíveis enquanto o vault está fechado.',
    editing: 'O agente aguarda enquanto o Obsidian está aberto para edição.',
    transition: 'Aguarde a confirmação do estado atual. O painel atualizará automaticamente.',
    error: 'A última mudança de modo foi interrompida. A recuperação usa a transição segura e não encerra o Obsidian à força.',
  };
  panelCopy.append(el('h2', '', modeTitle[status.mode]));
  panelCopy.append(el('p', 'muted', modeDescription[status.mode]));
  const actions = el('div', 'action-row');
  if (status.mode === 'agent') {
    const assume = el('button', 'button button-primary', 'Assumir edição');
    assume.disabled = inFlight || !status.editor_available;
    assume.addEventListener('click', () => runAction(assume, async () => {
      await request<Status>('/api/mode', { method: 'POST', body: JSON.stringify({ mode: 'editing' }) });
      window.location.assign('/obsidian/');
    }));
    actions.append(assume);
  } else if (status.mode === 'editing') {
    const openEditor = el('a', 'button button-primary', 'Abrir Obsidian');
    openEditor.href = '/obsidian/';
    openEditor.setAttribute('aria-label', 'Abrir Obsidian em nova página');
    openEditor.target = '_blank';
    openEditor.rel = 'noopener noreferrer';
    const finish = el('button', 'button button-secondary', 'Finalizar edição');
    finish.disabled = inFlight;
    finish.addEventListener('click', () => runAction(finish, async () => {
      currentStatus = await request<Status>('/api/mode', { method: 'POST', body: JSON.stringify({ mode: 'agent' }) });
      renderDashboard(currentStatus);
    }));
    actions.append(openEditor, finish);
  } else if (status.mode === 'error') {
    const recover = el('button', 'button button-primary', 'Recuperar modo agente');
    recover.disabled = inFlight;
    recover.addEventListener('click', () => runAction(recover, async () => {
      currentStatus = await request<Status>('/api/mode', { method: 'POST', body: JSON.stringify({ mode: 'agent' }) });
      renderDashboard(currentStatus);
    }));
    actions.append(recover);
  }
  statusCard.append(panelCopy, actions);

  const syncPanel = el('section', 'panel sync-panel');
  const syncHeader = el('div', 'section-heading');
  syncHeader.append(el('div', '', ''));
  const syncTitle = el('h2', '', 'Sincronização e backup');
  syncHeader.replaceChildren(syncTitle, el('span', `sync-state state-${status.sync.state.toLowerCase().replace(/[^a-z0-9-]/g, '-')}`, status.sync.message || status.sync.state));
  syncPanel.append(syncHeader);
  const syncButtons = el('div', 'action-row');
  const sync = el('button', 'button button-secondary', 'Sincronizar agora');
  sync.disabled = inFlight || status.mode !== 'agent';
  sync.addEventListener('click', () => runAction(sync, async () => {
    await request('/api/sync', { method: 'POST', body: '{}' });
    currentStatus = await request<Status>('/api/status');
    renderDashboard(currentStatus);
  }));
  const backup = el('button', 'button button-secondary', 'Criar backup');
  backup.disabled = inFlight || status.mode !== 'agent';
  backup.addEventListener('click', () => runAction(backup, async () => {
    await request('/api/backup', { method: 'POST', body: '{}' });
    currentStatus = await request<Status>('/api/status');
    renderDashboard(currentStatus);
  }));
  syncButtons.append(sync, backup);
  syncPanel.append(syncButtons);
  const phase = el('p', 'sync-detail', `Detalhes: ${status.sync.message || status.sync.state}`);
  syncPanel.append(phase);

  const phasePanel = el('section', 'phase-card');
  const phaseIcon = el('span', 'phase-icon', '✦');
  phaseIcon.setAttribute('aria-hidden', 'true');
  const phaseCopy = el('div', 'phase-copy');
  phaseCopy.append(el('p', 'eyebrow', 'PRÓXIMA ETAPA'));
  phaseCopy.append(el('h2', '', 'Assistente inteligente'));
  phaseCopy.append(el('p', 'muted', status.phase2.ready
    ? 'A próxima etapa está disponível para configuração.'
    : status.phase2.reason || 'Disponível após confirmar a elegibilidade da assinatura.'));
  const chatLink = el('a', 'button button-quiet', 'Abrir Gepeto');
  chatLink.href = '/chat';
  phasePanel.append(phaseIcon, phaseCopy, chatLink);

  const footer = el('footer', 'footer');
  const chatNav = el('a', '', 'Sobre o assistente');
  chatNav.href = '/chat';
  footer.append(el('span', '', 'Seus dados permanecem no seu ambiente privado.'), chatNav);

  if (alertText) setAlert(page, alertText);
  page.append(top, intro, metrics, statusCard, syncPanel, phasePanel, footer);
  app.append(page);
  pollTimer = window.setTimeout(() => void refreshStatus(), 10_000);
}

async function runAction(button: HTMLButtonElement, action: () => Promise<void>): Promise<void> {
  if (inFlight) return;
  inFlight = true;
  button.disabled = true;
  const original = button.textContent;
  let actionError = '';
  button.textContent = 'Aguarde…';
  try {
    await action();
  } catch (error) {
    if (error instanceof Error && error.message !== 'Sessão encerrada.') {
      actionError = error.message;
    }
  } finally {
    inFlight = false;
    if (actionError && csrfToken && location.pathname !== '/chat' && currentStatus) {
      renderDashboard(currentStatus, actionError);
    } else if (button.isConnected) {
      button.disabled = false;
      button.textContent = original;
    } else if (csrfToken && location.pathname !== '/chat' && currentStatus) renderDashboard(currentStatus, actionError);
  }
}

async function refreshStatus(): Promise<void> {
  if (inFlight || !csrfToken || location.pathname === '/chat') return;
  const generation = authGeneration;
  try {
    const status = await request<Status>('/api/status');
    if (generation !== authGeneration || !csrfToken) return;
    renderDashboard(status);
  } catch (error) {
    if (generation === authGeneration && error instanceof Error && error.message !== 'Sessão encerrada.' && currentStatus) {
      renderDashboard(currentStatus, error.message);
    }
  }
}

function showChat(): void {
  clearApp();
  activeViewCleanup = mountChat(app, request);
}

async function route(): Promise<void> {
  const generation = authGeneration;
  if (location.pathname === '/chat') {
    try {
      const session = await request<{ authenticated: boolean; csrf_token: string }>('/api/session');
      if (generation !== authGeneration) return;
      setAuthenticated(session.csrf_token);
      showChat();
    } catch { /* 401 route handler renders login. */ }
    return;
  }
  try {
    const status = await request<Status>('/api/status');
    if (generation !== authGeneration || !csrfToken) return;
    renderDashboard(status);
  } catch { /* 401 route handler renders login. */ }
}

async function start(): Promise<void> {
  try {
    const session = await request<{ authenticated: boolean; csrf_token: string }>('/api/session');
    setAuthenticated(session.csrf_token);
    await route();
  } catch {
    if (!csrfToken) showLogin();
  }
}

void start();
