"""Single-user Telegram polling and conservative outbound delivery."""
import asyncio
import json
import os
from pathlib import Path
import re
import stat
import time
import urllib.request

from .attachments import AttachmentError, MAX_BYTES


class TelegramError(Exception):
    pass


def message_chunks(text, limit=3500):
    chunk, units = [], 0
    for character in text:
        width = 2 if ord(character) > 0xffff else 1
        if units + width > limit:
            yield ''.join(chunk)
            chunk, units = [], 0
        chunk.append(character)
        units += width
    if chunk:
        yield ''.join(chunk)


class BotAPI:
    def __init__(self, token):
        self.token = token

    def call(self, method, values):
        if method not in {'getUpdates', 'getFile', 'sendMessage'}:
            raise TelegramError('Operação Telegram não permitida.')
        request = urllib.request.Request('https://api.telegram.org/bot' + self.token + '/' + method,
                                         data=json.dumps(values).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.loads(response.read(2 * 1024 * 1024))
            if not result.get('ok'):
                raise TelegramError('Telegram recusou o pedido.')
            return result['result']
        except Exception:
            # HTTP errors may include a token-bearing URL; never propagate them.
            raise TelegramError('Falha de conexão com Telegram.') from None

    def download(self, identifier):
        record = self.call('getFile', {'file_id': identifier})
        if record.get('file_size', 0) > MAX_BYTES:
            raise AttachmentError('Arquivo maior que 10 MiB.')
        path = record.get('file_path', '')
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in path.split('/') or path.startswith('/'):
            raise TelegramError('Telegram retornou caminho de arquivo inválido.')
        try:
            with urllib.request.urlopen('https://api.telegram.org/file/bot' + self.token + '/' + path, timeout=25) as response:
                data = response.read(MAX_BYTES + 1)
        except Exception:
            raise TelegramError('Não foi possível baixar o anexo do Telegram.') from None
        if len(data) > MAX_BYTES:
            raise AttachmentError('Arquivo maior que 10 MiB.')
        return data


def read_config(path):
    path = Path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027 or info.st_size > 4096:
            raise TelegramError('Configuração Telegram precisa de arquivo protegido, sem escrita pelo grupo ou acesso público.')
        record = json.load(source)
    if (not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', record.get('token', ''))
            or not isinstance(record.get('user_id'), int) or record['user_id'] <= 0):
        raise TelegramError('Configuração Telegram inválida.')
    return record


class TelegramService:
    def __init__(self, store, attachments, config_path, api_factory=BotAPI):
        self.store = store
        self.attachments = attachments
        self.config_path = Path(config_path)
        self.api_factory = api_factory
        self.task = None
        self.stopping = False
        self.next_send_at = 0
        with store.database.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS telegram_receipts (bot TEXT NOT NULL, update_id INTEGER NOT NULL, PRIMARY KEY(bot,update_id))')

    def status(self, state, message):
        self.store.database.set('telegram_status', {'state': state, 'message': message})

    def _wait_to_send(self):
        while time.monotonic() < self.next_send_at:
            if self.stopping:
                return False
            time.sleep(max(0, min(0.1, self.next_send_at - time.monotonic())))
        if self.stopping:
            return False
        self.next_send_at = time.monotonic() + 1.1
        return True

    def _send(self, api, values):
        if not self._wait_to_send():
            raise TelegramError('Envio interrompido.')
        return api.call('sendMessage', values)

    def process_update(self, update, config, api):
        identifier = update.get('update_id')
        if not isinstance(identifier, int):
            raise TelegramError('Atualização Telegram inválida.')
        bot = config.get('bot_username', config['token'].split(':')[0])
        with self.store.database.connect() as connection:
            if connection.execute('SELECT 1 FROM telegram_receipts WHERE bot=? AND update_id=?', (bot, identifier)).fetchone():
                return
        message = update.get('message', {})
        sender = message.get('from', {})
        chat = message.get('chat', {})
        authorized = (chat.get('type') == 'private' and sender.get('id') == config['user_id']
                      and chat.get('id') == config['user_id'] and not sender.get('is_bot'))
        if authorized:
            key = f'telegram:{bot}:{identifier}'
            if self.store.lookup_idempotency(key, origin='telegram'):
                self._receipt(bot, identifier)
                return
            text = str(message.get('text') or message.get('caption') or '')
            attachment_ids = []
            document = message.get('document')
            if not document and message.get('photo'):
                document = {**message['photo'][-1], 'file_name': 'telegram-photo.jpg'}
            if document:
                try:
                    if document.get('file_size', 0) > MAX_BYTES:
                        raise AttachmentError('Arquivo maior que 10 MiB.')
                    data = api.download(document['file_id'])
                    attachment_ids.append(self.attachments.add(document.get('file_name', 'attachment'), data)['id'])
                except AttachmentError as error:
                    # Persist visible feedback without asking the model to read rejected content.
                    self.status('attachment_rejected', str(error))
                    self._receipt(bot, identifier)
                    self._send(api, {'chat_id': config['user_id'], 'text': str(error)})
                    return
            if text or attachment_ids:
                try:
                    self.store.enqueue(text or 'Analise o anexo enviado.', origin='telegram',
                                       idempotency_key=key, attachment_ids=attachment_ids,
                                       external_reply={'chat_id': chat['id'], 'message_id': message.get('message_id')})
                except ValueError:
                    self.status('message_rejected', 'Mensagem excedeu os limites aceitos; divida o conteúdo e envie novamente.')
                    self._receipt(bot, identifier)
                    self._send(api, {'chat_id': config['user_id'], 'text': 'Mensagem excedeu os limites aceitos; divida o conteúdo e envie novamente.'})
                    return
        self._receipt(bot, identifier)

    def _receipt(self, bot, identifier):
        with self.store.database.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO telegram_receipts VALUES (?,?)', (bot, identifier))

    def deliver(self, api, config):
        for record in self.store.pending_outbox():
            reply = record['external_reply']
            if isinstance(reply, str):
                reply = json.loads(reply)
            if reply.get('chat_id') != config['user_id']:
                self.status('error', 'Destino pendente não corresponde à conta autorizada.')
                continue
            if not self.store.claim_outbox(record['id']):
                continue
            content = record['content'] or 'Execução concluída sem texto.'
            try:
                for index, chunk in enumerate(message_chunks(content)):
                    values = {'chat_id': config['user_id'], 'text': chunk}
                    if index == 0 and reply.get('message_id'):
                        values['reply_parameters'] = {'message_id': reply['message_id'], 'allow_sending_without_reply': True}
                    self._send(api, values)
                self.store.mark_outbox_sent(record['id'])
            except TelegramError:
                self.status('uncertain', 'Entrega Telegram incerta. Consulte a resposta no site; reenvio automático bloqueado.')
                return

    def poll_once(self):
        config = read_config(self.config_path)
        if config is None:
            self.status('not_configured', 'Configure o bot pelo terminal seguro da VPS.')
            return False
        bot = config.get('bot_username', config['token'].split(':')[0])
        offset_key = 'telegram_offset:' + bot
        api = self.api_factory(config['token'])
        self.deliver(api, config)
        offset = self.store.database.get(offset_key, config.get('initial_offset', 0))
        updates = api.call('getUpdates', {'offset': offset, 'timeout': 15, 'allowed_updates': ['message']})
        for update in updates:
            self.process_update(update, config, api)
            self.store.database.set(offset_key, update['update_id'] + 1)
        self.deliver(api, config)
        if self.store.database.get('telegram_status', {}).get('state') not in {'uncertain', 'attachment_rejected', 'message_rejected', 'error'}:
            self.status('connected', 'Telegram conectado à conta autorizada.')
        return True

    async def _run(self):
        while not self.stopping:
            try:
                connected = await asyncio.to_thread(self.poll_once)
                delay = 0.1 if connected else 5
            except Exception:
                self.status('offline', 'Telegram indisponível; histórico e fila continuam preservados.')
                delay = 15
            for _ in range(int(delay * 10)):
                if self.stopping:
                    break
                await asyncio.sleep(0.1)

    async def start(self):
        self.stopping = False
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.stopping = True
        if self.task is not None:
            await self.task
            self.task = None
