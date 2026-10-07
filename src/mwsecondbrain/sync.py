"""Execute the separately reviewed private vault synchronizer, never vault code."""
import json
import os
from pathlib import Path
import subprocess
import sys


def sync(vault: Path) -> dict:
    configured = os.environ.get("MWSB_SYNC_SCRIPT", "")
    script = Path(configured)
    if not configured or not script.is_absolute() or script.is_symlink() or not script.is_file() or script.resolve().is_relative_to(vault.resolve()):
        return {"state": "NOT_CONFIGURED", "message": "Sincronizador externo revisado ainda não configurado.", "pending": True}
    try:
        result = subprocess.run(
            [sys.executable, "-B", str(script), "--repo", str(vault), "reconcile"],
            capture_output=True, timeout=300, check=False,
        )
        record = json.loads(result.stdout)
        if not isinstance(record, dict) or not isinstance(record.get("state"), str) or not isinstance(record.get("message"), str):
            raise ValueError("Invalid synchronizer response")
        if record["state"] == "SYNCED" and (result.returncode != 0 or record.get("pending") is not False or record.get("remote_checked_now") is not True or not record.get("head")):
            raise ValueError("Unconfirmed synchronization")
        return record
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "ERROR", "message": "Sincronização interrompida ou retorno inválido; alterações preservadas.", "pending": True}
