"""Bounded subprocess decoder; output contains data, never executable instructions."""
import json
import resource
import sys
import warnings
from pathlib import Path

TEXT_LIMIT = 120_000


def extract(path, extension):
    if extension in {'.txt', '.md'}:
        data = path.read_bytes()
        if b'\0' in data:
            raise ValueError('Arquivo de texto contém dados binários.')
        text = data.decode('utf-8-sig')
        return {'text': text[:TEXT_LIMIT], 'partial': len(text) > TEXT_LIMIT, 'mime': 'text/plain' if extension == '.txt' else 'text/markdown'}
    if extension == '.pdf':
        from pypdf import PdfReader
        if not path.open('rb').read(5) == b'%PDF-':
            raise ValueError('Conteúdo não corresponde a PDF.')
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise ValueError('PDF criptografado não pode ser lido.')
        text = ''
        partial = len(reader.pages) > 200
        for page in reader.pages[:200]:
            text += (page.extract_text() or '') + '\n'
            if len(text) > TEXT_LIMIT:
                partial = True
                text = text[:TEXT_LIMIT]
                break
        if not text.strip():
            raise ValueError('PDF sem texto extraível. PDFs digitalizados precisam de OCR antes do envio.')
        return {'text': text, 'partial': partial, 'mime': 'application/pdf'}
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 25_000_000
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    formats = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}
    with Image.open(path) as picture:
        if picture.format != formats[extension]:
            raise ValueError('Conteúdo não corresponde à extensão da imagem.')
        picture.verify()
    with Image.open(path) as picture:
        picture.load()
    return {'text': '', 'partial': False, 'mime': Image.MIME[formats[extension]]}


def main():
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    try:
        result = extract(Path(sys.argv[1]), sys.argv[2])
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    except (ValueError, UnicodeError) as error:
        print(json.dumps({'ok': False, 'message': str(error)[:200]}))
    except Exception:
        print(json.dumps({'ok': False, 'message': 'Arquivo inválido ou excedeu o limite de leitura.'}))


if __name__ == '__main__':
    main()
