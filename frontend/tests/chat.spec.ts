import { expect, test, type Page, type Route } from '@playwright/test';

const initialChat = () => ({
  conversation_id: 'conv-1',
  capture_paused: false,
  messages: [
    { id: 'msg-1', role: 'user', content: 'Oi, Gepeto', origin: 'telegram', status: 'completed', created_at: '2026-10-07T12:00:00Z', attachments: [] },
    { id: 'msg-2', role: 'assistant', content: 'Olá! Posso ajudar.', origin: 'telegram', status: 'completed', created_at: '2026-10-07T12:00:02Z', attachments: [] },
  ],
  jobs: [] as { id: string; status: string; origin: string; message_id?: string; created_at?: string }[],
  operations: [{ id: 'op-1', status: 'queued', title: 'Rascunho de nota', message: 'Aguardando confirmação.' }],
  runtime: { state: 'ready', reason: '' },
});

type SetupOptions = {
  models?: { ready: boolean; models: { id: string; label: string; supports_images: boolean }[]; reason: string };
  chat?: ReturnType<typeof initialChat>;
  events?: (route: Route) => Promise<void>;
};

async function setupChat(page: Page, options: SetupOptions = {}) {
  const state = options.chat ?? initialChat();
  let capturePaused = state.capture_paused;
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, csrf_token: 'csrf-chat' } }));
  await page.route(url => new URL(url).pathname === '/api/chat', route => route.fulfill({ json: state }));
  await page.route('**/api/chat/conversations', route => route.fulfill({ json: { conversations: [
    { id: 'conv-1', created_at: '2026-10-07T12:00:00Z', active: true },
    { id: 'conv-0', created_at: '2026-10-06T12:00:00Z', active: false },
  ] } }));
  await page.route('**/api/models', route => route.fulfill({ json: options.models ?? {
    ready: true,
    models: [{ id: 'account-model-1', label: 'Modelo da conta', supports_images: true }],
    reason: '',
  } }));
  await page.route('**/api/chat/events', route => options.events
    ? options.events(route)
    : route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': keepalive\n\n' }));
  await page.route('**/api/chat/capture', async route => {
    const body = route.request().postDataJSON() as { paused: boolean };
    capturePaused = body.paused;
    state.capture_paused = body.paused;
    await route.fulfill({ json: { capture_paused: capturePaused } });
  });
  await page.goto('/chat');
  await expect(page.getByRole('heading', { name: 'Gepeto', exact: true })).toBeVisible();
  return { state, getCapturePaused: () => capturePaused };
}

test('shows Telegram source, queue, note operation, and account model', async ({ page }) => {
  await setupChat(page);

  await expect(page.getByText('Telegram').first()).toBeVisible();
  await expect(page.getByText('Origem: Web')).toBeVisible();
  await expect(page.getByText('Rascunho de nota')).toBeVisible();
  await expect(page.getByLabel('Modelo da conta')).toContainText('Modelo da conta');
  await expect(page.getByRole('button', { name: 'Captura de notas ativa' })).toHaveAttribute('aria-pressed', 'false');
});

test('opens Gepeto from the authenticated dashboard navigation', async ({ page }) => {
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, csrf_token: 'csrf-chat' } }));
  await page.route('**/api/status', route => route.fulfill({ json: {
    mode: 'agent', sync: { state: 'success', message: 'OK', last_synced_at: null },
    backup: { last_success_at: null }, phase2: { ready: true, reason: '' }, editor_available: true,
  } }));
  await page.route(url => new URL(url).pathname === '/api/chat', route => route.fulfill({ json: initialChat() }));
  await page.route('**/api/chat/conversations', route => route.fulfill({ json: { conversations: [] } }));
  await page.route('**/api/models', route => route.fulfill({ json: { ready: false, models: [], reason: 'Runtime indisponível para esta conta.' } }));

  await page.goto('/');
  await page.getByRole('link', { name: 'Abrir Gepeto' }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole('heading', { name: 'Gepeto', exact: true })).toBeVisible();
  await expect(page.getByText('Gepeto indisponível: Runtime indisponível para esta conta.')).toBeVisible();
});

test('clearly blocks sending when account model catalog is unavailable', async ({ page }) => {
  await setupChat(page, { models: { ready: false, models: [], reason: 'A autenticação da conta Gepeto precisa ser renovada.' } });

  await expect(page.getByText('Gepeto indisponível: A autenticação da conta Gepeto precisa ser renovada.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Enviar' })).toBeDisabled();
  await expect(page.getByRole('textbox', { name: 'Mensagem para Gepeto' })).toBeDisabled();
});

test('shows progressive SSE response after safe model send and restores one-turn capture pause', async ({ page }) => {
  const state = initialChat();
  let releaseEvent!: () => void;
  const eventGate = new Promise<void>(resolve => { releaseEvent = resolve; });
  let capturePausedDuringSend = false;
  await setupChat(page, {
    chat: state,
    events: async route => {
      await eventGate;
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'id: 31\ndata: {"id":"31","type":"delta","conversation_id":"conv-1","job_id":"job-1","delta":"Resposta progressiva"}\n\n',
      });
    },
  });
  await expect(page.getByLabel('Modelo da conta')).toBeEnabled();
  await page.getByLabel('Modelo da conta').selectOption('account-model-1');
  await page.route('**/api/chat/messages', async route => {
    const body = route.request().postDataJSON() as { text: string; model: string | null; idempotency_key: string };
    expect(body.text).toBe('Uma pergunta sem captura de nota');
    expect(body.model).toBe('account-model-1');
    expect(body.idempotency_key).toBeTruthy();
    capturePausedDuringSend = state.capture_paused;
    state.jobs = [{ id: 'job-1', status: 'running', origin: 'web', message_id: 'msg-new' }];
    await route.fulfill({ status: 202, json: { job_id: 'job-1', message_id: 'msg-new' } });
    releaseEvent();
  });

  await page.getByRole('textbox', { name: 'Mensagem para Gepeto' }).fill('Uma pergunta sem captura de nota');
  await page.getByLabel('Não guarde esta mensagem').check();
  await page.getByRole('button', { name: 'Enviar' }).click();

  await expect(page.getByText('Resposta progressiva')).toBeVisible();
  await expect.poll(() => capturePausedDuringSend).toBe(true);
  await expect.poll(() => state.capture_paused).toBe(false);
  await expect(page.getByRole('button', { name: 'Captura de notas ativa' })).toHaveAttribute('aria-pressed', 'false');
});

test('cancels active queue job with CSRF and refreshes its state', async ({ page }) => {
  const state = initialChat();
  state.jobs = [{ id: 'job-active', status: 'running', origin: 'telegram', created_at: '2026-10-07T12:01:00Z' }];
  await setupChat(page, { chat: state });
  let token = '';
  await page.route('**/api/chat/cancel', async route => {
    token = route.request().headers()['x-csrf-token'] ?? '';
    expect(route.request().headers().origin).toBe('http://127.0.0.1:4173');
    expect(route.request().postDataJSON()).toEqual({ job_id: 'job-active' });
    state.jobs[0].status = 'cancelled';
    await route.fulfill({ json: { cancel_requested: true } });
  });

  await page.getByRole('button', { name: 'Cancelar resposta' }).click();
  await expect(page.getByText('Pedido de cancelamento enviado.')).toBeVisible();
  await expect(page.getByText('Cancelada')).toBeVisible();
  expect(token).toBe('csrf-chat');
});

test('starts a new conversation through the protected endpoint', async ({ page }) => {
  const state = initialChat();
  await setupChat(page, { chat: state });
  await page.route('**/api/chat/new', async route => {
    expect(route.request().headers()['x-csrf-token']).toBe('csrf-chat');
    expect(route.request().headers().origin).toBe('http://127.0.0.1:4173');
    state.conversation_id = 'conv-2';
    state.messages = [];
    state.jobs = [];
    state.operations = [];
    await route.fulfill({ json: { conversation_id: 'conv-2' } });
  });
  await page.route('**/api/chat/conversations', route => route.fulfill({ json: { conversations: [
    { id: 'conv-2', created_at: '2026-10-07T12:15:00Z', active: true },
    { id: 'conv-1', created_at: '2026-10-07T12:00:00Z', active: false },
  ] } }));

  await page.getByRole('button', { name: 'Nova conversa' }).click();
  await expect(page.getByRole('combobox', { name: 'Selecionar conversa' })).toHaveValue('conv-2');
  await expect(page.getByText('Fila vazia')).toBeVisible();
  await expect(page.getByText('Nenhuma alteração de nota sugerida.')).toBeVisible();
});

test('uploads valid attachment, rejects oversize file, and sends attachment id', async ({ page }) => {
  const state = initialChat();
  await setupChat(page, { chat: state });
  let uploadCount = 0;
  let sentIds: string[] = [];
  await page.route('**/api/attachments', async route => {
    uploadCount += 1;
    expect(route.request().headers()['content-type']).toContain('multipart/form-data');
    await route.fulfill({ json: { id: 'attachment-1', name: 'leitura.txt', mime: 'text/plain', size: 7, status: 'ready', message: '' } });
  });
  await page.route('**/api/chat/messages', async route => {
    const body = route.request().postDataJSON() as { attachment_ids: string[] };
    sentIds = body.attachment_ids;
    state.jobs = [{ id: 'job-file', status: 'queued', origin: 'web', message_id: 'msg-file' }];
    await route.fulfill({ status: 202, json: { job_id: 'job-file', message_id: 'msg-file' } });
  });

  await page.locator('#chat-attachment').setInputFiles({
    name: 'grande.txt', mimeType: 'text/plain', buffer: Buffer.alloc(10 * 1024 * 1024 + 1),
  });
  await expect(page.getByRole('alert')).toHaveText('grande.txt excede o limite de 10 MiB.');
  expect(uploadCount).toBe(0);

  await page.locator('#chat-attachment').setInputFiles({ name: 'leitura.txt', mimeType: 'text/plain', buffer: Buffer.from('conteúdo') });
  await expect(page.getByText(/leitura.txt.*Enviado/)).toBeVisible();
  await page.getByRole('textbox', { name: 'Mensagem para Gepeto' }).fill('Leia este arquivo.');
  await page.getByRole('button', { name: 'Enviar' }).click();
  await expect.poll(() => sentIds.length).toBe(1);
  expect(sentIds).toEqual(['attachment-1']);
  expect(uploadCount).toBe(1);
});

test('keeps attachment upload rejection visible and does not send it', async ({ page }) => {
  await setupChat(page);
  let messageRequests = 0;
  await page.route('**/api/attachments', async route => {
    expect(route.request().headers()['x-csrf-token']).toBe('csrf-chat');
    await route.fulfill({ status: 415, json: { detail: 'Este PDF não contém texto compatível.' } });
  });
  await page.route('**/api/chat/messages', async route => {
    messageRequests += 1;
    await route.fulfill({ status: 202, json: { job_id: 'never', message_id: 'never' } });
  });

  await page.locator('#chat-attachment').setInputFiles({ name: 'digitalizado.pdf', mimeType: 'application/pdf', buffer: Buffer.from('pdf') });
  await expect(page.getByRole('alert')).toHaveText('Este PDF não contém texto compatível.');
  await expect(page.getByText(/digitalizado.pdf.*Aguardando envio/)).toBeVisible();
  expect(messageRequests).toBe(0);
});

test('returns to login when chat mutation session expires', async ({ page }) => {
  await setupChat(page);
  await page.route('**/api/chat/messages', route => route.fulfill({ status: 401, json: { detail: 'expired' } }));

  await page.getByRole('textbox', { name: 'Mensagem para Gepeto' }).fill('Oi');
  await page.getByRole('button', { name: 'Enviar' }).click();
  await expect(page.getByRole('heading', { name: 'MWSecondBrain' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveText('Sua sessão terminou. Entre novamente para continuar.');
  await expect(page.getByRole('heading', { name: 'Gepeto', exact: true })).toHaveCount(0);
});
