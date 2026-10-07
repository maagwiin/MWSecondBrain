type ChatRequest = <T>(path: string, init?: RequestInit) => Promise<T>;
type Origin = 'web' | 'telegram';
type Attachment = { id: string; name: string; mime: string; size: number; status: string; message?: string };
type Message = {
  id: string;
  role: string;
  content: string;
  origin: Origin;
  status: string;
  created_at: string;
  attachments: (Attachment | string)[];
};
type Job = {
  id: string;
  status: string;
  origin?: Origin;
  created_at?: string;
  message_id?: string;
  message?: string;
  error?: string;
  capture_denied?: boolean;
};
type NoteOperation = {
  id: string;
  status: string;
  path?: string;
  message?: string;
  title?: string;
  error?: string;
  reason?: string;
};
type Conversation = { id: string; created_at: string; active: boolean };
type ChatSnapshot = {
  conversation_id: string;
  capture_paused: boolean;
  messages: Message[];
  jobs: Job[];
  operations: NoteOperation[];
  runtime?: { state: string; reason: string };
};
type ModelCatalog = { ready: boolean; models: { id: string; label: string; supports_images: boolean }[]; reason: string };
type EventPayload = { id?: string; type: string; conversation_id: string; job_id?: string; delta?: string };

const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const ACCEPTED_TYPES = new Set(['application/pdf', 'image/png', 'image/jpeg', 'image/webp', 'text/plain', 'text/markdown']);

function node<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = ''): HTMLElementTagNameMap[K] {
  const item = document.createElement(tag);
  if (className) item.className = className;
  item.textContent = text;
  return item;
}

function formatDate(value?: string): string {
  if (!value) return 'Agora';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Data indisponível';
  return new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short', timeZone: 'America/Sao_Paulo' }).format(date);
}

function labelStatus(status: string): string {
  const labels: Record<string, string> = {
    queued: 'Na fila', running: 'Em andamento', completed: 'Concluída', failed: 'Falhou',
    cancelled: 'Cancelada', uncertain: 'Resultado incerto', pending: 'Pendente', applied: 'Aplicada',
    rejected: 'Recusada', waiting: 'Aguardando', ready: 'Pronto', paused: 'Pausado', error: 'Indisponível',
  };
  return labels[status] ?? status;
}

function isActive(status: string): boolean {
  return status === 'queued' || status === 'running';
}

function normalizeAttachment(attachment: Attachment | string): Attachment {
  return typeof attachment === 'string'
    ? { id: attachment, name: 'Anexo enviado', mime: '', size: 0, status: 'queued' }
    : attachment;
}

export function mountChat(root: HTMLElement, request: ChatRequest): () => void {
  let closed = false;
  let actionBusy = false;
  let loadGeneration = 0;
  let snapshot: ChatSnapshot | null = null;
  let conversations: Conversation[] = [];
  let catalog: ModelCatalog = { ready: false, models: [], reason: 'Carregando modelos da conta…' };
  let pendingFiles: File[] = [];
  let readyAttachments: Attachment[] = [];
  let selectedModelId = '';
  let lastEventId = '';
  let streamingJobId = '';
  let streamingText = '';
  let streamController: AbortController | undefined;
  let reconnectTimer: number | undefined;
  let optimisticText = '';

  const page = node('main', 'chat-page');
  const topbar = node('header', 'chat-topbar');
  const home = node('a', 'chat-brand', 'MW SecondBrain');
  home.href = '/';
  const topActions = node('div', 'chat-top-actions');
  const accountStatus = node('span', 'chat-account-status', 'Conta Gepeto');
  const newConversation = node('button', 'chat-button chat-button-quiet', 'Nova conversa');
  newConversation.type = 'button';
  topActions.append(accountStatus, newConversation);
  topbar.append(home, topActions);

  const layout = node('div', 'chat-layout');
  const sidebar = node('aside', 'chat-sidebar');
  sidebar.setAttribute('aria-label', 'Conversas e fila');
  const sidebarHeading = node('div', 'chat-side-heading');
  sidebarHeading.append(node('p', 'chat-eyebrow', 'GEPETO'), node('h1', '', 'Conversas'));
  const historyLabel = node('label', 'chat-label', 'Histórico');
  historyLabel.htmlFor = 'conversation-history';
  const history = node('select', 'chat-select');
  history.id = 'conversation-history';
  history.setAttribute('aria-label', 'Selecionar conversa');
  const captureControl = node('button', 'capture-control');
  captureControl.type = 'button';
  captureControl.setAttribute('aria-pressed', 'false');
  const captureDot = node('span', 'capture-dot');
  captureDot.setAttribute('aria-hidden', 'true');
  captureControl.append(captureDot, node('span', 'capture-label', 'Captura de notas ativa'));
  const queueTitle = node('h2', 'chat-sidebar-title', 'Fila de trabalho');
  const queueList = node('div', 'queue-list');
  sidebar.append(sidebarHeading, historyLabel, history, captureControl, queueTitle, queueList);

  const mainColumn = node('section', 'chat-main');
  mainColumn.setAttribute('aria-label', 'Conversa com Gepeto');
  const chatHeading = node('div', 'chat-heading');
  const headingCopy = node('div');
  headingCopy.append(node('p', 'chat-eyebrow', 'SEU ASSISTENTE PESSOAL'), node('h2', '', 'Gepeto'));
  const sourceIndicator = node('span', 'source-indicator', 'Origem: Web');
  chatHeading.append(headingCopy, sourceIndicator);
  const alert = node('div', 'chat-alert');
  alert.setAttribute('role', 'alert');
  alert.hidden = true;
  const runtimeNotice = node('div', 'runtime-notice');
  runtimeNotice.hidden = true;
  const resumeButton = node('button', 'chat-button chat-button-quiet', 'Retomar Gepeto');
  resumeButton.type = 'button';
  const historyNotice = node('p', 'model-notice', 'Histórico somente leitura. Selecione a conversa ativa para enviar mensagens.');
  historyNotice.hidden = true;
  const modelNotice = node('div', 'model-notice');
  modelNotice.hidden = true;
  const transcript = node('div', 'chat-transcript');
  transcript.setAttribute('role', 'log');
  transcript.setAttribute('aria-live', 'polite');
  transcript.setAttribute('aria-relevant', 'additions text');
  const operationsPanel = node('section', 'operations-panel');
  operationsPanel.setAttribute('aria-label', 'Estado das notas sugeridas');
  const operationsTitle = node('h3', '', 'Notas sugeridas');
  const operationsList = node('div', 'operations-list');
  operationsPanel.append(operationsTitle, operationsList);
  const composer = node('form', 'chat-composer');
  const attachLabel = node('label', 'chat-button chat-button-quiet attach-label', 'Anexar arquivo');
  attachLabel.htmlFor = 'chat-attachment';
  const fileInput = node('input', 'visually-hidden');
  fileInput.id = 'chat-attachment';
  fileInput.type = 'file';
  fileInput.accept = '.pdf,.png,.jpg,.jpeg,.webp,.txt,.md,application/pdf,image/png,image/jpeg,image/webp,text/plain,text/markdown';
  const textArea = node('textarea', 'chat-input');
  textArea.id = 'chat-message';
  textArea.rows = 3;
  textArea.maxLength = 20_000;
  textArea.placeholder = 'Escreva para Gepeto…';
  textArea.setAttribute('aria-label', 'Mensagem para Gepeto');
  const attachmentTray = node('div', 'attachment-tray');
  const composerTools = node('div', 'composer-tools');
  const noCaptureLabel = node('label', 'capture-once-label');
  const noCapture = node('input');
  noCapture.type = 'checkbox';
  noCaptureLabel.append(noCapture, node('span', '', 'Não guarde esta mensagem'));
  const sendButton = node('button', 'chat-button chat-button-primary', 'Enviar');
  sendButton.type = 'submit';
  const sendRow = node('div', 'composer-send-row');
  const charCount = node('span', 'char-count', '0 / 20.000');
  sendRow.append(charCount, sendButton);
  composerTools.append(noCaptureLabel, sendRow);
  composer.append(fileInput, textArea, attachmentTray, attachLabel, composerTools);

  mainColumn.append(chatHeading, modelNotice, runtimeNotice, historyNotice, alert, transcript, operationsPanel, composer);
  layout.append(sidebar, mainColumn);
  page.append(topbar, layout);
  root.replaceChildren(page);

  function setAlert(message = ''): void {
    alert.hidden = !message;
    alert.textContent = message;
  }

  function updateControls(): void {
    const unavailable = !catalog.ready;
    const activeJob = snapshot?.jobs.find(job => isActive(job.status));
    const historical = isHistorical();
    const blocked = actionBusy || unavailable || Boolean(activeJob) || historical;
    sendButton.disabled = blocked;
    sendButton.textContent = actionBusy ? 'Aguarde…' : activeJob ? 'Aguarde a fila' : 'Enviar';
    textArea.disabled = blocked;
    fileInput.disabled = blocked;
    noCapture.disabled = blocked;
    attachLabel.setAttribute('aria-disabled', String(fileInput.disabled));
    captureControl.disabled = actionBusy || historical;
    historyNotice.hidden = !historical;
    resumeButton.disabled = actionBusy;
    const modelSelect = composer.querySelector<HTMLSelectElement>('#chat-model-select');
    if (modelSelect) modelSelect.disabled = blocked || catalog.models.length === 0;
    newConversation.disabled = actionBusy;
    history.disabled = actionBusy;
  }

  function isHistorical(): boolean {
    const active = conversations.find(conversation => conversation.active);
    return Boolean(active && snapshot && snapshot.conversation_id !== active.id);
  }

  function showModelState(): void {
    modelNotice.hidden = catalog.ready;
    modelNotice.textContent = catalog.ready
      ? ''
      : `Gepeto indisponível: ${catalog.reason || 'nenhum modelo foi liberado para esta conta.'}`;
    composer.querySelector('.model-picker-label')?.remove();
    const modelLabel = node('label', 'model-picker-label', 'Modelo da conta');
    modelLabel.htmlFor = 'chat-model-select';
    const modelSelect = node('select', 'chat-select model-select');
    modelSelect.id = 'chat-model-select';
    modelSelect.setAttribute('aria-label', 'Modelo da conta');
    const auto = node('option', '', 'Automático');
    auto.value = '';
    modelSelect.append(auto);
    for (const model of catalog.models) {
      const option = node('option', '', model.label);
      option.value = model.id;
      modelSelect.append(option);
    }
    modelSelect.disabled = !catalog.ready || catalog.models.length === 0;
    modelSelect.value = selectedModelId;
    modelSelect.addEventListener('change', () => { selectedModelId = modelSelect.value; });
    modelLabel.append(modelSelect);
    attachLabel.before(modelLabel);
    updateControls();
  }

  function renderConversations(): void {
    const selected = snapshot?.conversation_id ?? conversations.find(item => item.active)?.id ?? '';
    const options = conversations.map(conversation => {
      const option = node('option', '', `${conversation.active ? 'Ativa · ' : ''}${formatDate(conversation.created_at)}`);
      option.value = conversation.id;
      option.selected = conversation.id === selected;
      return option;
    });
    history.replaceChildren(...options);
    if (!options.length) {
      const empty = node('option', '', 'Nenhuma conversa');
      empty.value = '';
      history.append(empty);
    }
  }

  function addAttachmentLink(parent: HTMLElement, reference: Attachment | string): void {
    const attachment = normalizeAttachment(reference);
    const link = node('a', 'attachment-link', `↧ ${attachment.name}${attachment.size ? ` · ${formatSize(attachment.size)}` : ''}`);
    link.href = `/api/attachments/${encodeURIComponent(attachment.id)}`;
    link.setAttribute('aria-label', `Baixar anexo ${attachment.name}`);
    parent.append(link);
    if (attachment.message) parent.append(node('span', 'attachment-warning', attachment.message));
  }

  function renderMessages(): void {
    const fragment = document.createDocumentFragment();
    const messages = snapshot?.messages ?? [];
    for (const message of messages) {
      const card = node('article', `message-card message-${message.role === 'user' ? 'user' : 'assistant'}`);
      const meta = node('div', 'message-meta');
      const speaker = message.role === 'user' ? 'Você' : message.role === 'assistant' ? 'Gepeto' : 'Sistema';
      meta.append(node('strong', '', speaker));
      meta.append(node('span', 'origin-pill', message.origin === 'telegram' ? 'Telegram' : 'Web'));
      meta.append(node('time', '', formatDate(message.created_at)));
      if (message.status && message.status !== 'completed') meta.append(node('span', 'message-status', labelStatus(message.status)));
      card.append(meta, node('p', 'message-content', message.content ?? ''));
      if (message.attachments?.length) {
        const attachments = node('div', 'message-attachments');
        for (const attachment of message.attachments) addAttachmentLink(attachments, attachment);
        card.append(attachments);
      }
      fragment.append(card);
    }
    const active = snapshot?.jobs.find(job => isActive(job.status));
    const partialAssistant = messages.some(message => message.role === 'assistant' && message.status === 'running');
    if (active && streamingJobId === active.id && streamingText && !partialAssistant) {
      const live = node('article', 'message-card message-assistant message-streaming');
      live.append(node('div', 'message-meta', 'Gepeto · respondendo'), node('p', 'message-content', streamingText));
      fragment.append(live);
    } else if (active && !messages.some(message => message.role === 'assistant' && message.status === 'running')) {
      const waiting = node('article', 'message-card message-assistant message-waiting');
      waiting.append(node('div', 'message-meta', 'Gepeto · na fila'), node('p', 'message-content', 'Preparando resposta…'));
      fragment.append(waiting);
    }
    if (optimisticText) {
      const optimistic = node('article', 'message-card message-user');
      optimistic.append(node('div', 'message-meta', 'Você · enviando'), node('p', 'message-content', optimisticText));
      fragment.append(optimistic);
    }
    transcript.replaceChildren(fragment);
    transcript.scrollTop = transcript.scrollHeight;
    sourceIndicator.textContent = active?.origin === 'telegram' ? 'Origem: Telegram' : 'Origem: Web';
    renderQueue();
  }

  function renderQueue(): void {
    const jobs = snapshot?.jobs ?? [];
    if (!jobs.length) {
      queueList.replaceChildren(node('p', 'empty-note', 'Fila vazia'));
      updateControls();
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const job of jobs) {
      const item = node('article', 'queue-item');
      const row = node('div', 'queue-item-top');
      const title = job.origin === 'telegram' ? 'Telegram' : 'Web';
      row.append(node('strong', '', title), node('span', `queue-status queue-${job.status}`, labelStatus(job.status)));
      item.append(row, node('time', '', formatDate(job.created_at)));
      if (job.message) item.append(node('p', 'queue-message', job.message));
      if (job.error) item.append(node('p', 'queue-message', job.error));
      if (isActive(job.status)) {
        const cancel = node('button', 'text-button', 'Cancelar resposta');
        cancel.type = 'button';
        cancel.disabled = actionBusy;
        cancel.addEventListener('click', () => void runMutation(async () => {
          await request('/api/chat/cancel', { method: 'POST', body: JSON.stringify({ job_id: job.id }) });
          setAlert('Pedido de cancelamento enviado.');
          await loadSnapshot(snapshot?.conversation_id);
        }));
        item.append(cancel);
      } else if (job.status === 'uncertain') {
        const retry = node('button', 'text-button', 'Tentar novamente');
        retry.type = 'button';
        retry.disabled = actionBusy || isHistorical() || Boolean(activeJob());
        retry.addEventListener('click', () => void explicitRetry(job));
        item.append(retry, node('p', 'uncertain-note', 'A execução anterior pode ter começado. Nada será repetido sem sua confirmação.'));
      }
      fragment.append(item);
    }
    queueList.replaceChildren(fragment);
    updateControls();
  }

  function renderOperations(): void {
    const operations = snapshot?.operations ?? [];
    if (!operations.length) {
      operationsList.replaceChildren(node('p', 'empty-note', 'Nenhuma alteração de nota sugerida.'));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const operation of operations) {
      const item = node('article', 'operation-item');
      const title = operation.title || operation.path || 'Sugestão de nota';
      item.append(node('strong', '', title), node('span', `operation-state operation-${operation.status}`, labelStatus(operation.status)));
      if (operation.message) item.append(node('p', 'operation-message', operation.message));
      if (operation.reason) item.append(node('p', 'operation-message', operation.reason));
      if (operation.error) item.append(node('p', 'operation-message', operation.error));
      fragment.append(item);
    }
    operationsList.replaceChildren(fragment);
  }

  function renderSnapshot(): void {
    if (!snapshot) return;
    captureControl.setAttribute('aria-pressed', String(snapshot.capture_paused));
    captureControl.classList.toggle('capture-paused', snapshot.capture_paused);
    captureControl.querySelector('.capture-label')!.textContent = snapshot.capture_paused
      ? 'Captura de notas pausada' : 'Captura de notas ativa';
    runtimeNotice.hidden = !snapshot.runtime?.reason && !snapshot.runtime?.state;
    runtimeNotice.replaceChildren(node('span', '', snapshot.runtime?.reason || `Runtime: ${labelStatus(snapshot.runtime?.state ?? 'ready')}`));
    if (['paused_quota', 'auth_required', 'paused'].includes(snapshot.runtime?.state ?? '')) runtimeNotice.append(resumeButton);
    renderConversations();
    renderMessages();
    renderOperations();
  }

  function formatSize(size: number): string {
    return size < 1024 * 1024 ? `${Math.max(1, Math.round(size / 1024))} KB` : `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderPendingFiles(): void {
    const fragment = document.createDocumentFragment();
    for (const [index, file] of pendingFiles.entries()) {
      const item = node('span', 'pending-attachment', `${file.name} · ${formatSize(file.size)} · Aguardando envio`);
      const remove = node('button', 'remove-attachment', 'Remover');
      remove.type = 'button';
      remove.setAttribute('aria-label', `Remover ${file.name}`);
      remove.addEventListener('click', () => {
        pendingFiles.splice(index, 1);
        renderPendingFiles();
      });
      item.append(remove);
      fragment.append(item);
    }
    for (const attachment of readyAttachments) {
      const item = node('span', 'pending-attachment uploaded-attachment', `${attachment.name} · ${formatSize(attachment.size)} · Enviado`);
      if (attachment.message) item.append(node('span', 'attachment-warning', attachment.message));
      const remove = node('button', 'remove-attachment', 'Remover');
      remove.type = 'button';
      remove.setAttribute('aria-label', `Remover ${attachment.name}`);
      remove.addEventListener('click', () => {
        readyAttachments = readyAttachments.filter(current => current.id !== attachment.id);
        renderPendingFiles();
      });
      item.append(remove);
      fragment.append(item);
    }
    attachmentTray.replaceChildren(fragment);
  }

  async function runMutation(operation: () => Promise<void>): Promise<void> {
    if (actionBusy || closed) return;
    actionBusy = true;
    setAlert('');
    updateControls();
    try {
      await operation();
    } catch (error) {
      if (error instanceof Error && error.message !== 'Sessão encerrada.' && error.message !== 'Resposta de uma sessão anterior.') {
        setAlert(error.message);
      }
    } finally {
      actionBusy = false;
      if (!closed) renderQueue();
    }
  }

  async function uploadPendingFiles(): Promise<void> {
    for (const file of [...pendingFiles]) {
      validateFile(file);
      const form = new FormData();
      form.append('file', file);
      const uploaded = await request<Attachment>('/api/attachments', { method: 'POST', body: form });
      if (!['ready', 'completed', 'partial'].includes(uploaded.status)) {
        throw new Error(uploaded.message || `O arquivo ${file.name} não ficou pronto para envio.`);
      }
      readyAttachments.push(uploaded);
      pendingFiles = pendingFiles.filter(pending => pending !== file);
      renderPendingFiles();
    }
  }

  function validateFile(file: File): void {
    if (file.size > MAX_ATTACHMENT_BYTES) throw new Error(`${file.name} excede o limite de 10 MiB.`);
    const extension = file.name.split('.').pop()?.toLowerCase();
    const allowedExtensions = new Set(['pdf', 'png', 'jpg', 'jpeg', 'webp', 'txt', 'md']);
    if (!ACCEPTED_TYPES.has(file.type) && !allowedExtensions.has(extension ?? '')) {
      throw new Error(`${file.name}: tipo de arquivo não permitido. Use PDF, PNG, JPEG, WebP, TXT ou Markdown.`);
    }
    const selectedModel = catalog.models.find(model => model.id === selectedModelId);
    if (file.type.startsWith('image/') && selectedModel && !selectedModel.supports_images) {
      throw new Error(`O modelo ${selectedModel.label} não aceita imagens.`);
    }
  }

  async function submitMessage(text: string, attachments: (Attachment | string)[] = readyAttachments, dontCapture = noCapture.checked): Promise<void> {
    if (isHistorical()) throw new Error('Histórico somente leitura. Selecione a conversa ativa.');
    if (activeJob()) throw new Error('A fila já tem uma resposta em andamento. Aguarde ou cancele antes de enviar.');
    if (!catalog.ready) throw new Error(catalog.reason || 'Nenhum modelo está disponível para esta conta.');
    if (!text.trim() && !attachments.length) throw new Error('Escreva uma mensagem ou anexe um arquivo.');
    await uploadPendingFiles();
    streamingJobId = '';
    streamingText = '';
    optimisticText = text;
    renderMessages();
    try {
      const modelSelect = composer.querySelector<HTMLSelectElement>('#chat-model-select');
      const result = await request<{ job_id: string; message_id: string }>('/api/chat/messages', {
        method: 'POST',
        body: JSON.stringify({
          text,
          attachment_ids: attachments.map(attachment => normalizeAttachment(attachment).id),
          model: modelSelect?.value || null,
          idempotency_key: crypto.randomUUID(),
          no_capture: dontCapture,
        }),
      });
      if (streamingJobId !== result.job_id) {
        streamingJobId = result.job_id;
        streamingText = '';
      }
      noCapture.checked = false;
      readyAttachments = [];
      textArea.value = '';
      charCount.textContent = '0 / 20.000';
      renderPendingFiles();
    } finally {
      optimisticText = '';
      if (!closed) await loadSnapshot(snapshot?.conversation_id);
    }
  }

  function activeJob(): Job | undefined {
    return snapshot?.jobs.find(job => isActive(job.status));
  }

  async function explicitRetry(job: Job): Promise<void> {
    await runMutation(async () => {
      const source = snapshot?.messages.find(message => message.id === job.message_id && message.role === 'user');
      if (!source) throw new Error('Mensagem original não está disponível para nova tentativa.');
      await submitMessage(source.content, source.attachments ?? [], job.capture_denied ?? true);
    });
  }

  async function loadSnapshot(conversationId?: string): Promise<void> {
    const generation = ++loadGeneration;
    const query = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : '';
    const data = await request<ChatSnapshot>(`/api/chat${query}`);
    if (closed || generation !== loadGeneration) return;
    snapshot = data;
    snapshot.messages = snapshot.messages.map(message => ({ ...message, attachments: (message.attachments ?? []).map(normalizeAttachment) }));
    if (!activeJob() || activeJob()?.status === 'completed') {
      streamingJobId = '';
      streamingText = '';
    }
    renderSnapshot();
  }

  async function loadConversationList(): Promise<void> {
    const data = await request<{ conversations: Conversation[] }>('/api/chat/conversations');
    if (closed) return;
    conversations = data.conversations;
    renderConversations();
    renderQueue();
  }

  async function loadModels(): Promise<void> {
    try {
      catalog = await request<ModelCatalog>('/api/models');
    } catch (error) {
      if (error instanceof Error && error.message !== 'Sessão encerrada.') {
        catalog = { ready: false, models: [], reason: error.message };
      }
    }
    if (!closed) showModelState();
  }

  async function refreshAll(conversationId?: string): Promise<void> {
    setAlert('');
    try {
      await Promise.all([loadSnapshot(conversationId), loadConversationList(), loadModels()]);
    } catch (error) {
      if (error instanceof Error && error.message !== 'Sessão encerrada.' && error.message !== 'Resposta de uma sessão anterior.') setAlert(error.message);
    }
  }

  async function chooseConversation(id: string): Promise<void> {
    if (!id || id === snapshot?.conversation_id) return;
    await runMutation(async () => {
      streamingJobId = '';
      streamingText = '';
      optimisticText = '';
      await refreshAll(id);
    });
  }

  async function createConversation(): Promise<void> {
    await runMutation(async () => {
      if (activeJob() && !window.confirm('A resposta atual será cancelada. Criar conversa nova?')) return;
      const result = await request<{ conversation_id: string }>('/api/chat/new', { method: 'POST', body: '{}' });
      streamingJobId = '';
      streamingText = '';
      optimisticText = '';
      readyAttachments = [];
      pendingFiles = [];
      textArea.value = '';
      charCount.textContent = '0 / 20.000';
      renderPendingFiles();
      await refreshAll(result.conversation_id);
      textArea.focus();
    });
  }

  async function toggleCapture(): Promise<void> {
    if (!snapshot || isHistorical()) return;
    await runMutation(async () => {
      await request('/api/chat/capture', {
        method: 'POST',
        body: JSON.stringify({ paused: !snapshot?.capture_paused }),
      });
      await loadSnapshot(snapshot?.conversation_id);
    });
  }

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const text = textArea.value;
    await runMutation(() => submitMessage(text));
  }

  async function handleFiles(): Promise<void> {
    const selected = Array.from(fileInput.files ?? []);
    fileInput.value = '';
    try {
      for (const file of selected) validateFile(file);
      pendingFiles.push(...selected);
      renderPendingFiles();
      await runMutation(uploadPendingFiles);
    } catch (error) {
      if (error instanceof Error) setAlert(error.message);
    }
  }

  function handleEvent(event: EventPayload): void {
    if (event.id) lastEventId = event.id;
    if (event.conversation_id !== snapshot?.conversation_id) return;
    if (event.job_id && event.delta) {
      if (streamingJobId !== event.job_id) streamingText = '';
      streamingJobId = event.job_id;
      streamingText += event.delta;
    }
    if (event.type === 'error') setAlert('A conexão de eventos informou uma falha. Atualizando estado…');
    void loadSnapshot(snapshot.conversation_id).catch(error => {
      if (error instanceof Error && error.message !== 'Sessão encerrada.') setAlert(error.message);
    });
  }

  function parseFrame(frame: string): void {
    let eventId = '';
    const data: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith('id:')) eventId = line.slice(3).trim();
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    try {
      const event = JSON.parse(data.join('\n')) as EventPayload;
      if (eventId) event.id = eventId;
      handleEvent(event);
    } catch {
      setAlert('Recebi um evento inválido. O histórico continua disponível.');
    }
  }

  async function listenToEvents(): Promise<void> {
    if (closed) return;
    streamController = new AbortController();
    try {
      const headers = new Headers({ Accept: 'text/event-stream' });
      if (lastEventId) headers.set('Last-Event-ID', lastEventId);
      const response = await fetch('/api/chat/events', { headers, credentials: 'same-origin', signal: streamController.signal });
      if (response.status === 401) {
        try { await request('/api/session'); } catch { /* Session handler opens login. */ }
        return;
      }
      if (!response.ok || !response.body) throw new Error(`Eventos indisponíveis (${response.status}).`);
      accountStatus.textContent = 'Conta Gepeto';
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!closed) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let splitAt = buffer.search(/\r?\n\r?\n/);
        while (splitAt >= 0) {
          const frame = buffer.slice(0, splitAt);
          const separator = buffer.slice(splitAt).match(/^\r?\n\r?\n/)?.[0] ?? '\n\n';
          buffer = buffer.slice(splitAt + separator.length);
          parseFrame(frame);
          splitAt = buffer.search(/\r?\n\r?\n/);
        }
      }
      if (!closed) throw new Error('A conexão de eventos foi encerrada. Reconectando…');
    } catch (error) {
      if (!closed && error instanceof Error && error.name !== 'AbortError') {
        accountStatus.textContent = 'Reconectando eventos…';
        reconnectTimer = window.setTimeout(() => void listenToEvents(), 3000);
      }
    }
  }

  newConversation.addEventListener('click', () => void createConversation());
  history.addEventListener('change', () => void chooseConversation(history.value));
  captureControl.addEventListener('click', () => void toggleCapture());
  resumeButton.addEventListener('click', () => void runMutation(async () => {
    await request('/api/chat/resume', { method: 'POST', body: '{}' });
    await refreshAll(snapshot?.conversation_id);
  }));
  composer.addEventListener('submit', event => void handleSubmit(event as SubmitEvent));
  fileInput.addEventListener('change', () => void handleFiles());
  textArea.addEventListener('input', () => { charCount.textContent = `${textArea.value.length.toLocaleString('pt-BR')} / 20.000`; });
  textArea.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });

  showModelState();
  setAlert('');
  accountStatus.textContent = 'Conectando à conta…';
  void refreshAll().then(() => {
    if (!closed) {
      accountStatus.textContent = 'Conta Gepeto';
      void listenToEvents();
    }
  });

  return () => {
    closed = true;
    loadGeneration += 1;
    streamController?.abort();
    window.clearTimeout(reconnectTimer);
  };
}
