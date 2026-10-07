"""Harmless deployment check. Uses a dummy token and never starts a model turn."""
from pathlib import Path
import subprocess
import tempfile

from mwsecondbrain.codex import CodexRuntime, TOOL_SCHEMAS, _Session

with tempfile.TemporaryDirectory(prefix='mwsb-isolation-') as temporary:
    root = Path(temporary)
    auth = root / 'oauth'
    auth.mkdir(mode=0o700)
    runtime = CodexRuntime(auth, root / 'snapshot')
    (root / 'outside').write_text('fixture private data')
    (runtime.work_dir / 'note.md').write_text('fixture note')
    check = "from pathlib import Path; assert Path('/workspace/note.md').read_text()=='fixture note'; assert not Path(" + repr(str(root / 'outside')) + ").exists(); Path('/workspace/note.md').write_text('blocked')"
    result = subprocess.run(runtime._confined_command(['/usr/bin/python3', '-c', check]),
                            env=runtime._child_environment('unused-smoke-token'), capture_output=True)
    assert result.returncode != 0 and b'Read-only file system' in result.stderr, 'Confinement check failed'
    session = _Session(runtime._start_process('unused-smoke-token'), lambda *_: {}, lambda _: None, lambda: False)
    try:
        session.rpc('initialize', {'clientInfo': {'name': 'MWSecondBrain', 'version': '0.1.0'}, 'capabilities': {'experimentalApi': True}})
        session.send({'method': 'initialized', 'params': {}})
        result = session.rpc('thread/start', {'cwd': '/workspace', 'model': 'fixture-model', 'approvalPolicy': 'never', 'sandbox': 'read-only',
            'dynamicTools': [{'type': 'namespace', 'name': 'brain', 'description': 'Controlled tools',
                'tools': [{'type': 'function', 'name': name, 'description': name, 'inputSchema': schema} for name, schema in TOOL_SCHEMAS.items()]}]})
        assert result['cwd'] == '/workspace' and result['thread']['id']
    finally:
        session.close()
    assert session.process.poll() is not None
print('Service-user confinement and Codex startup passed. No inference requested.')
