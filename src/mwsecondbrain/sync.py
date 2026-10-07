"""Execute the separately reviewed private vault synchronizer, never vault code."""
import json
import os
import signal
from pathlib import Path
import subprocess
import sys

TIMEOUT_SECONDS = 300


def _invoke(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=TIMEOUT_SECONDS)
        return process.returncode, stdout
    finally:
        # A killed wrapper must not leave Git or its descendants writing after the lease ends.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)


def sync(vault: Path) -> dict:
    configured = os.environ.get("MWSB_SYNC_SCRIPT", "")
    script = Path(configured)
    if not configured or not script.is_absolute() or script.is_symlink() or not script.is_file() or script.resolve().is_relative_to(vault.resolve()):
        return {"state": "NOT_CONFIGURED", "message": "Sincronizador externo revisado ainda não configurado.", "pending": True}
    try:
        code, stdout = _invoke([sys.executable, "-B", str(script), "--repo", str(vault), "reconcile"])
        record = json.loads(stdout)
        if not isinstance(record, dict) or not isinstance(record.get("state"), str) or not isinstance(record.get("message"), str):
            raise ValueError("Invalid synchronizer response")
        if record["state"] == "SYNCED" and (code != 0 or record.get("pending") is not False or record.get("remote_checked_now") is not True or not record.get("head")):
            raise ValueError("Unconfirmed synchronization")
        return record
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "ERROR", "message": "Sincronização interrompida ou retorno inválido; alterações preservadas.", "pending": True}
