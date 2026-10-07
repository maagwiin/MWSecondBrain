import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfWriter
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from mwsecondbrain.attachments import Attachments, AttachmentError, MAX_BYTES, router
from mwsecondbrain.db import Database


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = Attachments(Database(self.root / 'state'), self.root / 'files')

    def test_partial_text_is_explicit_and_original_download_is_preserved(self):
        payload = ('á' * 120001).encode()
        result = self.manager.add('../../note.md', payload)
        self.assertEqual(result['name'], 'note.md')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual((self.root / 'files' / result['id']).read_bytes(), payload)
        self.assertEqual(len(self.manager.record(result['id'])['text']), 120000)

    def test_extension_spoof_and_binary_text_rejected_without_orphan_file(self):
        for name, data in [('photo.png', b'%PDF-1.7 fake'), ('note.txt', b'\0binary'), ('file.exe', b'content'), ('bad.pdf', b'not a PDF')]:
            with self.subTest(name=name), self.assertRaises(AttachmentError):
                self.manager.add(name, data)
        self.assertEqual(list((self.root / 'files').iterdir()), [])

    def test_non_bmp_unicode_is_within_valid_text_limit(self):
        result = self.manager.add('unicode.txt', ('😀' * 100000).encode())
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(self.manager.record(result['id'])['text']), 100000)

    def test_size_limit_before_decode(self):
        with self.assertRaises(AttachmentError), patch('mwsecondbrain.attachments.subprocess.run') as decoder:
            self.manager.add('note.txt', b'a' * (MAX_BYTES + 1))
        decoder.assert_not_called()

    def test_pdf_without_text_and_encrypted_pdf_have_clear_errors(self):
        writer = PdfWriter()
        writer.add_blank_page(72, 72)
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(AttachmentError, 'sem texto'):
            self.manager.add('scan.pdf', output.getvalue())
        writer.encrypt('private')
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(AttachmentError, 'criptografado'):
            self.manager.add('encrypted.pdf', output.getvalue())

    def test_real_images_decode_and_copy_to_turn_snapshot(self):
        for extension, format in [('.png', 'PNG'), ('.jpg', 'JPEG'), ('.webp', 'WEBP')]:
            with self.subTest(extension=extension):
                stream = io.BytesIO()
                Image.new('RGB', (8, 8), 'red').save(stream, format=format)
                record = self.manager.add('picture' + extension, stream.getvalue())
                work = self.root / format
                work.mkdir()
                resolved = self.manager.resolve([record['id']], work)
                self.assertTrue(resolved[0]['image_path'].is_relative_to(work))
                self.assertEqual(resolved[0]['image_path'].read_bytes(), stream.getvalue())

    def test_download_never_follows_symlink(self):
        record = self.manager.add('note.txt', b'hello')
        path = self.root / 'files' / record['id']
        path.unlink()
        path.symlink_to('/etc/passwd')
        with self.assertRaises(AttachmentError):
            self.manager.record(record['id'])

    def test_decoder_timeout_leaves_no_attachment(self):
        import subprocess
        with patch('mwsecondbrain.attachments.subprocess.run', side_effect=subprocess.TimeoutExpired('decoder', 15)):
            with self.assertRaises(AttachmentError):
                self.manager.add('note.txt', b'hello')
        self.assertEqual(list((self.root / 'files').iterdir()), [])

    def test_http_authentication_and_upload_limits(self):
        app = FastAPI()
        def auth(request: Request):
            if request.headers.get('x-test-auth') != 'yes':
                raise HTTPException(401)
        app.include_router(router(self.manager, auth, auth))
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/attachments', files={'file': ('x.txt', b'ok')}).status_code, 401)
            result = client.post('/api/attachments', headers={'x-test-auth': 'yes'}, files={'file': ('x.txt', b'ok')})
            self.assertEqual(result.status_code, 200, result.text)
            identifier = result.json()['id']
            self.assertEqual(client.get('/api/attachments/' + identifier).status_code, 401)
            response = client.get('/api/attachments/' + identifier, headers={'x-test-auth': 'yes'})
            self.assertEqual(response.content, b'ok')
            self.assertIn('attachment;', response.headers['content-disposition'])
            self.assertEqual(client.post('/api/attachments', headers={'x-test-auth': 'yes'}, files={'file': ('x.txt', b'x' * (MAX_BYTES + 1))}).status_code, 413)
