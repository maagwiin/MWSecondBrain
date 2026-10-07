# Implementation contracts

Phase 1 is independently usable. Phase 2 is gated on a real SIWC subscription inference. Do not simulate eligibility, install a paid key, or label unfinished chat as functional.

## HTTP

- GET /api/session: 200 {authenticated:true, csrf_token:string}, or 401.
- POST /api/login: {password:string}; session cookie, {csrf_token:string}. Invalid 401, throttled 429.
- POST /api/logout: CSRF-protected; revokes current session.
- GET /api/status: {mode:'agent'|'editing'|'transition'|'error', sync:{state:string,message:string,last_synced_at:string|null}, backup:{last_success_at:string|null}, phase2:{ready:boolean,reason:string}, editor_available:boolean}.
- POST /api/mode: {mode:'editing'|'agent'}, CSRF required. Returns status; 409 on unsafe transition.
- POST /api/sync: CSRF required; 409 during editing, otherwise returns sync result.
- POST /api/backup: CSRF required; 409 during editing, otherwise result.
- GET /api/proxy-auth: validate session for Caddy forward_auth; reject unless editing mode; no redirect on WebSocket/API.
- GET /healthz: {status:'ok'}, no private data.

Errors: {detail:string}. Mutations require X-CSRF-Token and Origin matching configured public origin. Cookie mwsb_session: HttpOnly, Secure, SameSite=Strict. No public registration/reset endpoint. Local CLI handles initial password/reset with getpass. Login limit five failures per IP per 15 minutes, bounded records. Session idle timeout 12h, absolute 7d.

## Python

Package src/mwsecondbrain. Config via MWSB_* variables. Controller persists mode in SQLite and uses flock across operations; failed/uncertain transitions fail closed. Controller owns synchronization/backup/editor switching. Adapter command is fixed deployment helper with only start/stop/status verbs, never arbitrary shell commands from HTTP. Backend systemd service runs as dedicated user; no Docker socket mounted in app.

Sync callback sync(vault:Path)->dict {state,message,head?,pending}. Backup callback backup(vault:Path,state_dir:Path,backup_dir:Path)->dict. Frontend does not infer successful sync from local saves. Use UTC timestamps for state, America/Sao_Paulo for daily scheduling at 03:00.

The sync adapter invokes a root-installed copy of the private `brain_sync.py` and `brain_policy.py` outside the mutable vault. The private utility adds `reconcile` without changing existing commands; its tests use temporary Git repositories. At every write, scanning and explicit path selection remain required. No force push, reset --hard, stash or automatic rebase.

Logout revokes the session before attempting graceful editor shutdown. Caddy closes proxied streams after 30 seconds, bounding access by an already-established WebSocket after revocation or expiration. The browser transport must reconnect and pass authentication again. Failed shutdown leaves the vault blocked for recovery.

No sensitive values in logs; no production domain or repository identity in public code. UI in Portuguese, keyboard accessible, vanilla TypeScript, no React required. Production frontend served by FastAPI; Vite development proxy only for local tests.
