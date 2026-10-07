import { expect, test } from '@playwright/test';

const status = {
  mode: 'agent',
  sync: { state: 'success', message: 'Tudo sincronizado', last_synced_at: '2026-10-07T12:00:00Z' },
  backup: { last_success_at: '2026-10-07T11:00:00Z' },
  phase2: { ready: false, reason: 'Aguardando confirmação SIWC.' },
  editor_available: true,
};

async function installSession(page: import('@playwright/test').Page): Promise<void> {
  await page.route('**/api/session', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ authenticated: true, csrf_token: 'test-csrf' }),
  }));
}

test('shows action error after busy state ends', async ({ page }) => {
  await installSession(page);
  await page.route('**/api/status', route => route.fulfill({ json: status }));
  let csrfHeader = '';
  await page.route('**/api/sync', async route => {
    csrfHeader = route.request().headers()['x-csrf-token'] ?? '';
    await route.fulfill({
    status: 409,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'Sincronização recusada durante edição.' }),
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Sincronizar agora' }).click();
  await expect(page.getByRole('alert')).toHaveText('Sincronização recusada durante edição.');
  await expect(page.getByRole('button', { name: 'Sincronizar agora' })).toBeEnabled();
  expect(csrfHeader).toBe('test-csrf');
});

test('ignores pending status response after logout', async ({ page }) => {
  await installSession(page);
  let statusCalls = 0;
  let releaseStatus!: () => void;
  let finishStatus!: () => void;
  const delayed = new Promise<void>(resolve => { releaseStatus = resolve; });
  const responseComplete = new Promise<void>(resolve => { finishStatus = resolve; });
  await page.route('**/api/status', async route => {
    statusCalls += 1;
    if (statusCalls === 2) {
      await delayed;
      await route.fulfill({ json: status });
      finishStatus();
      return;
    }
    await route.fulfill({ json: status });
  });
  await page.route('**/api/logout', route => route.fulfill({ status: 204 }));

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Seu espaço, em equilíbrio.' })).toBeVisible();
  await page.waitForTimeout(10_100);
  await expect.poll(() => statusCalls).toBe(2);
  await page.getByRole('button', { name: 'Sair' }).click();
  await expect(page.getByRole('heading', { name: 'MWSecondBrain' })).toBeVisible();
  releaseStatus();
  await responseComplete;
  await expect(page.getByRole('heading', { name: 'MWSecondBrain' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Assumir edição' })).toHaveCount(0);
});

test('clears authenticated dashboard when mutation returns expired-session 401', async ({ page }) => {
  await installSession(page);
  await page.route('**/api/status', route => route.fulfill({ json: status }));
  await page.route('**/api/sync', route => route.fulfill({ status: 401, json: { detail: 'Expired' } }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Sincronizar agora' }).click();
  await expect(page.getByRole('heading', { name: 'MWSecondBrain' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Assumir edição' })).toHaveCount(0);
});

test('recovers persisted error mode and keeps conflict visible', async ({ page }) => {
  await installSession(page);
  await page.route('**/api/status', route => route.fulfill({ json: { ...status, mode: 'error' } }));
  let csrfHeader = '';
  await page.route('**/api/mode', async route => {
    csrfHeader = route.request().headers()['x-csrf-token'] ?? '';
    await route.fulfill({ status: 409, json: { detail: 'O estado do editor ainda não permite recuperação.' } });
  });

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'O modo precisa de recuperação' })).toBeVisible();
  await expect(page.getByText('A recuperação usa a transição segura e não encerra o Obsidian à força.')).toBeVisible();
  await page.getByRole('button', { name: 'Recuperar modo agente' }).click();
  await expect(page.getByRole('alert')).toHaveText('O estado do editor ainda não permite recuperação.');
  await expect(page.getByRole('heading', { name: 'O modo precisa de recuperação' })).toBeVisible();
  expect(csrfHeader).toBe('test-csrf');
});
