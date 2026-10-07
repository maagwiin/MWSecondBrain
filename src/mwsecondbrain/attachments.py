"""Authenticated attachments stored outside Git and decoded in a bounded process."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

MAX_BYTES = 10 * 1024 * 1024
EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.txt', '.md'}


class AttachmentError(ValueError):
    pass


class Attachments:
    def __init__(self, database, root):
        self.database = database
        self.root = Path(root)
        if self.root.is_symlink():
            raise AttachmentError('Diretório de anexos inválido.')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with database.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL,
                size INTEGER NOT NULL, text TEXT NOT NULL, partial INTEGER NOT NULL,
                created_at REAL NOT NULL)''')

    def add(self, name, data):
        name = str(name).replace('\\', '/').split('/')[-1][:200]
        extension = Path(name).suffix.lower()
        if extension not in EXTENSIONS:
            raise AttachmentError('Formato não permitido. Use PDF, PNG, JPEG, WebP, TXT ou Markdown.')
        if not data or len(data) > MAX_BYTES:
            raise AttachmentError('Arquivo vazio ou maior que 10 MiB.')
        identifier = uuid.uuid4().hex
        path = self.root / identifier
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(data)
            result = subprocess.run([sys.executable, '-I', str(Path(__file__).with_name('extract_attachment.py')), str(path), extension],
                                    capture_output=True, timeout=15, check=False)
            if result.returncode or len(result.stdout) > 1_000_000:
                raise AttachmentError('Leitura excedeu o limite de tempo ou memória.')
            record = json.loads(result.stdout)
            if not record.get('ok'):
                raise AttachmentError(record.get('message', 'Arquivo inválido.'))
            with self.database.connect() as connection:
                connection.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?,?)',
                                   (identifier, name, record['mime'], len(data), record['text'], int(record['partial']), time.time()))
            return self.metadata(identifier)
        except Exception as error:
            path.unlink(missing_ok=True)
            if isinstance(error, AttachmentError):
                raise
            raise AttachmentError('Não foi possível ler o anexo com segurança.') from None

    def record(self, identifier):
        if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise AttachmentError('Anexo inexistente.')
        with self.database.connect() as connection:
            row = connection.execute('SELECT * FROM attachments WHERE id=?', (identifier,)).fetchone()
        path = self.root / identifier
        if row is None or path.is_symlink() or not path.is_file():
            raise AttachmentError('Anexo inexistente.')
        return dict(row)

    def metadata(self, identifier):
        record = self.record(identifier)
        return {key: record[key] for key in ('id', 'name', 'mime', 'size')} | {
            'status': 'partial' if record['partial'] else 'ready',
            'message': 'Leitura parcial: documento excede os limites de páginas ou texto.' if record['partial'] else 'Pronto para leitura.'}

    def resolve(self, identifiers, work_dir):
        if len(identifiers) > 10:
            raise AttachmentError('Envie no máximo dez anexos por mensagem.')
        records = []
        destination = Path(work_dir) / '.attachments'
        destination.mkdir(mode=0o700, exist_ok=True)
        if destination.is_symlink():
            raise AttachmentError('Destino de anexos inválido.')
        for identifier in dict.fromkeys(identifiers):
            record = self.record(identifier)
            item = self.metadata(identifier) | {'text': record['text'], 'partial': bool(record['partial'])}
            if record['mime'].startswith('image/'):
                suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[record['mime']]
                target = destination / (identifier + suffix)
                if target.exists() or target.is_symlink():
                    raise AttachmentError('Destino de imagem já existe.')
                # Server-generated filenames only; decode completed before storage.
                with (self.root / identifier).open('rb') as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                item['image_path'] = target
            records.append(item)
        return records


def router(manager, require_session, require_mutation):
    routes = APIRouter()
    upload_lock = asyncio.Lock()

    @routes.post('/api/attachments')
    async def upload(request: Request, current=Depends(require_mutation)):
        async with upload_lock:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BYTES + 65536:
                    raise HTTPException(413, 'Arquivo maior que 10 MiB.')
            request._body = bytes(body)
            try:
                async with request.form(max_files=1, max_fields=0) as form:
                    file = form.get('file')
                    if file is None or not hasattr(file, 'read'):
                        raise HTTPException(400, 'Envie um arquivo no campo file.')
                    data = await file.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise HTTPException(413, 'Arquivo maior que 10 MiB.')
                    return await asyncio.to_thread(manager.add, file.filename, data)
            except AttachmentError as error:
                raise HTTPException(400, str(error)) from None

    @routes.get('/api/attachments/{identifier}')
    def download(identifier: str, current=Depends(require_session)):
        try:
            record = manager.record(identifier)
        except AttachmentError:
            raise HTTPException(404, 'Anexo inexistente.') from None
        return FileResponse(manager.root / identifier, media_type=record['mime'], filename=record['name'],
                            content_disposition_type='attachment', headers={'Cache-Control': 'no-store'})

    return routes
