import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mwsecondbrain.db import Database
from mwsecondbrain.telegram import TelegramService, TelegramError, read_config, message_chunks


class Store:
    def __init__(self, root):
        self.database = Database(root / 'state')
        self.jobs = {}
        self.outbox = []

    def lookup_idempotency(self, key, origin='telegram'):
        return self.jobs.get(key)

    def enqueue(self, text, **values):
        if len(text) > 50000:
            raise ValueError('too large')
        self.jobs.setdefault(values['idempotency_key'], {'text': text, **values})
        return {'job_id': 1, 'message_id': 1}

    def pending_outbox(self):
        return [item for item in self.outbox if item['status'] == 'pending']

    def claim_outbox(self, identifier):
        row = next(item for item in self.outbox if item['id'] == identifier)
        if row['status'] != 'pending':
            return False
        row['status'] = 'uncertain'
        return True

    def mark_outbox_sent(self, identifier):
        next(item for item in self.outbox if item['id'] == identifier)['status'] = 'sent'


class API:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.fail_send = False

    def call(self, method, values):
        self.calls.append((method, values))
        if method == 'sendMessage' and self.fail_send:
            raise TelegramError('offline')
        return self.updates if method == 'getUpdates' else {}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.api = API()
        self.config = {'token': '123:' + 'x' * 30, 'user_id': 12345, 'bot_username': 'example_bot', 'initial_offset': 5}
        self.path = self.root / 'telegram.json'
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)
        self.service = TelegramService(self.store, None, self.path, api_factory=lambda token: self.api)
        self.delay = patch.object(self.service, '_wait_to_send', return_value=True)
        self.delay.start()
        self.addCleanup(self.delay.stop)

    def update(self, identifier=5, user=12345, kind='private', text='Olá'):
        return {'update_id': identifier, 'message': {'message_id': 10, 'from': {'id': user}, 'chat': {'id': user, 'type': kind}, 'text': text}}

    def test_only_explicit_user_private_chat_can_enqueue(self):
        for update in [self.update(user=999), self.update(identifier=6, kind='group'), self.update(identifier=7, kind='supergroup')]:
            self.service.process_update(update, self.config, self.api)
        self.assertFalse(self.store.jobs)
        self.assertFalse(self.api.calls)
        self.service.process_update(self.update(identifier=8), self.config, self.api)
        self.assertEqual(len(self.store.jobs), 1)

    def test_duplicate_updates_and_restart_preserve_offset(self):
        self.api.updates = [self.update()]
        self.service.poll_once()
        self.assertEqual(self.store.database.get('telegram_offset:example_bot'), 6)
        restarted = TelegramService(self.store, None, self.path, api_factory=lambda token: self.api)
        restarted.poll_once()
        self.assertEqual(len(self.store.jobs), 1)
        self.assertEqual(self.api.calls[-1][1]['offset'], 6)

    def test_enqueue_dedup_covers_crash_before_receipt(self):
        self.store.enqueue('previous', origin='telegram', idempotency_key='telegram:example_bot:5')
        self.service.process_update(self.update(), self.config, self.api)
        self.assertEqual(len(self.store.jobs), 1)
        self.assertEqual(next(iter(self.store.jobs.values()))['text'], 'previous')

    def test_uncertain_send_never_retries_blindly(self):
        self.store.outbox = [{'id': 1, 'external_reply': {'chat_id': 12345, 'message_id': 10}, 'content': 'reply', 'status': 'pending'}]
        self.api.fail_send = True
        self.service.deliver(self.api, self.config)
        self.assertEqual(self.store.outbox[0]['status'], 'uncertain')
        self.service.deliver(self.api, self.config)
        self.assertEqual(len(self.api.calls), 1)

    def test_sends_only_authorized_destination_and_splits_long_reply(self):
        self.store.outbox = [{'id': 1, 'external_reply': {'chat_id': 999}, 'content': 'private', 'status': 'pending'},
                             {'id': 2, 'external_reply': {'chat_id': 12345}, 'content': 'a' * 7001, 'status': 'pending'}]
        self.service.deliver(self.api, self.config)
        self.assertEqual(len(self.api.calls), 3)
        self.assertTrue(all(call[1]['chat_id'] == 12345 for call in self.api.calls))
        self.assertEqual(self.store.outbox[1]['status'], 'sent')

    def test_oversize_message_is_acknowledged_without_blocking_following_updates(self):
        self.api.updates = [self.update(text='x' * 50001), self.update(identifier=6)]
        self.service.poll_once()
        self.assertEqual(self.store.database.get('telegram_offset:example_bot'), 7)
        self.assertEqual(len(self.store.jobs), 1)

    def test_configuration_missing_world_readable_or_symlink(self):
        self.assertIsNone(read_config(self.root / 'missing'))
        self.path.chmod(0o644)
        with self.assertRaises(TelegramError):
            read_config(self.path)
        link = self.root / 'link'
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            read_config(link)

    def test_emoji_chunks_preserve_content_and_telegram_utf16_limit(self):
        content = '😀Olá' * 2000
        chunks = list(message_chunks(content))
        self.assertEqual(''.join(chunks), content)
        self.assertTrue(all(len(chunk.encode('utf-16-le')) // 2 <= 3500 for chunk in chunks))

    def test_shutdown_during_rate_limit_stops_delivery(self):
        self.store.outbox = [{'id': 1, 'external_reply': {'chat_id': 12345}, 'content': 'reply', 'status': 'pending'}]
        with patch.object(self.service, '_wait_to_send', return_value=False):
            self.service.deliver(self.api, self.config)
        self.assertFalse(self.api.calls)
        self.assertEqual(self.store.outbox[0]['status'], 'uncertain')
