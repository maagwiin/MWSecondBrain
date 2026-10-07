#!/usr/bin/env python3
"""Pair an explicitly confirmed private Telegram account; never first-user wins."""
import getpass
import grp
import json
import os
from pathlib import Path
import re
import secrets
import time
import urllib.request


def api(token, method, values):
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
                                     data=json.dumps(values).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise ValueError('Telegram recusou o pedido.')
    return result['result']


def main():
    if os.geteuid() != 0:
        raise ValueError('Execute como administrador local da VPS.')
    path = Path('/etc/mwsecondbrain/telegram.json')
    if path.exists():
        raise ValueError('Configuração já existe. Revise a conta antes de substituir o arquivo local.')
    token = getpass.getpass('Token do BotFather (oculto): ').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Formato do token inválido.')
    bot = api(token, 'getMe', {})
    nonce = secrets.token_urlsafe(12)
    phrase = '/vincular ' + nonce
    print('Abra no Telegram: https://t.me/' + bot['username'])
    print('Envie esta mensagem NO PRIVADO para seu bot: ' + phrase)
    print('Aguardando por até cinco minutos. Nenhuma conta será liberada sem sua confirmação.')
    deadline = time.monotonic() + 300
    offset = 0
    candidate = None
    while time.monotonic() < deadline and candidate is None:
        updates = api(token, 'getUpdates', {'offset': offset, 'timeout': 15, 'allowed_updates': ['message']})
        for update in updates:
            offset = max(offset, update['update_id'] + 1)
            message = update.get('message', {})
            if (message.get('text') == phrase and message.get('chat', {}).get('type') == 'private'
                    and message.get('from', {}).get('id') == message.get('chat', {}).get('id')
                    and not message.get('from', {}).get('is_bot')):
                candidate = message['from']
                break
    if candidate is None:
        raise ValueError('Prazo encerrado; nenhuma conta autorizada.')
    identifier = candidate['id']
    print('Conta encontrada: ' + str(candidate.get('first_name', '')) + ' @' + str(candidate.get('username', '')))
    print('ID numérico: ' + str(identifier))
    if input('Para autorizar esta conta, digite CONFIRMAR ' + str(identifier) + ': ').strip() != 'CONFIRMAR ' + str(identifier):
        raise ValueError('Confirmação recusada; nenhuma conta autorizada.')
    record = {'token': token, 'user_id': identifier, 'initial_offset': offset, 'bot_username': bot['username']}
    temporary = path.with_name('.telegram-' + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, 0, grp.getgrnam('mwsb').gr_gid)
        os.chmod(temporary, 0o640)
        # Hard link refuses to replace a configuration created concurrently.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print('Token salvo em arquivo protegido. Conta autorizada explicitamente. Não compartilhe esse arquivo.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyboardInterrupt, EOFError) as error:
        print(str(error) or 'Configuração cancelada.')
        raise SystemExit(1)
    except Exception:
        print('Falha na conexão ou gravação. Nenhum token foi exibido; confira a configuração local.')
        raise SystemExit(1)
