"""Validated application configuration; no credentials in environment defaults."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    vault: Path
    state_dir: Path
    backup_dir: Path
    public_origin: str
    frontend_dir: Path | None = None
    obsidian_config_dir: Path | None = None

    def __post_init__(self):
        parsed = urlsplit(self.public_origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("MWSB_PUBLIC_ORIGIN must be an HTTPS origin without a path")
        # Separate roots prevent auth state and archives from entering the vault.
        roots = []
        for name in ("vault", "state_dir", "backup_dir"):
            value = Path(getattr(self, name)).resolve()
            if value == Path("/"):
                raise ValueError(f"{name} cannot be filesystem root")
            object.__setattr__(self, name, value)
            roots.append(value)
        for index, root in enumerate(roots):
            for other in roots[index + 1:]:
                if root == other or root in other.parents or other in root.parents:
                    raise ValueError("Vault, state and backup directories must be separate")
        if self.frontend_dir is not None:
            object.__setattr__(self, "frontend_dir", Path(self.frontend_dir).resolve())
        if self.obsidian_config_dir is not None:
            object.__setattr__(self, "obsidian_config_dir", Path(self.obsidian_config_dir).resolve())

    @classmethod
    def from_env(cls):
        required = ("MWSB_VAULT_DIR", "MWSB_STATE_DIR", "MWSB_BACKUP_DIR", "MWSB_PUBLIC_ORIGIN")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError("Required environment settings missing: " + ", ".join(missing))
        frontend = os.environ.get("MWSB_FRONTEND_DIR")
        if not frontend:
            candidate = Path(__file__).resolve().parents[2] / "frontend" / "dist"
            frontend = str(candidate) if candidate.is_dir() else None
        return cls(vault=Path(os.environ[required[0]]), state_dir=Path(os.environ[required[1]]),
                   backup_dir=Path(os.environ[required[2]]), public_origin=os.environ[required[3]],
                   frontend_dir=Path(frontend) if frontend else None,
                   obsidian_config_dir=Path(os.environ["MWSB_OBSIDIAN_CONFIG_DIR"])
                   if os.environ.get("MWSB_OBSIDIAN_CONFIG_DIR") else None)
