# Phase 2 contracts

The subscription gate passed on the deployment account. Generic tests use simulators; only final acceptance may invoke a real model again. The personal agent is named Gepeto. No API key, alternate provider or billing fallback is accepted.

## HTTP

All endpoints require the existing session. Mutations use the existing Origin and CSRF checks.

- `GET /api/chat?conversation_id=...`: `{conversation_id, capture_paused, messages, jobs, operations}`. Without an ID, returns the active conversation. Messages: `{id, role, content, origin, status, created_at, attachments}`; origin is `web` or `telegram`; status is `queued`, `running`, `completed`, `failed`, `cancelled` or `uncertain`.
- `GET /api/chat/conversations`: `{conversations:[{id,created_at,active}]}`.
- `POST /api/chat/messages`: `{text, attachment_ids:[], model:null, idempotency_key}`. Returns `{job_id,message_id}` with 202. Same key returns the existing result.
- `POST /api/chat/cancel`: `{job_id}`; requests interruption and returns `{cancel_requested:true}`.
- `POST /api/chat/new`: starts a new active conversation after cancelling any active turn; `{conversation_id}`. Old history remains readable.
- `POST /api/chat/capture`: `{paused:boolean}`. Persisted for the active conversation. Explicit “não guarde” disables capture for that turn as well.
- `GET /api/chat/events`: authenticated SSE, `data` events `{id,type,conversation_id,job_id?,delta?}`; durable monotonic IDs and `Last-Event-ID` supported. A client may reload `/api/chat` after any event.
- `GET /api/models`: `{ready,models:[{id,label,supports_images}],reason}`. Only account-catalog models appear; never hard-code development-model names as runtime choices.
- `POST /api/attachments`: multipart field `file`; returns `{id,name,mime,size,status,message}`. Maximum 10 MiB; PDF text, PNG, JPEG, WebP, TXT and Markdown only.
- `GET /api/attachments/{id}`: authenticated download with safe Content-Disposition. Attachment IDs, not client paths, select stored files.

The UI must show queue, progressive response, source, attachment state and note-operation state. Availability failures remain visible. Uncertain jobs after a restart require an explicit user retry; never replay automatically.

## Runtime ownership

SQLite owns the conversation, jobs, events, attachments and proposed note operations. One worker consumes jobs in order across web and Telegram. Runtime calls Codex App Server using stdio in a dedicated process environment. OAuth registration and refresh use a dedicated protected directory, never global Codex credentials. Quota exhaustion pauses processing. Tools exposing shell, network browsing, arbitrary files or server administration are disabled. Runtime tools only search/read a consistent vault snapshot and propose controlled Markdown additions/updates.

The controller alone applies note changes under its existing exclusive lock after confirming the editor stopped. Each proposal records the original content hash and new content. Deletions, instruction changes, traversal, symlinks and stale bases are rejected. During editing, proposals wait and the runtime reads the snapshot taken before Obsidian opened. On recovery, a file already matching the proposed content is recognized rather than applied again. Successful writes trigger conservative synchronization.

Telegram uses long polling, accepts only the configured numeric user in a private chat, persists the update offset and deduplication key, and sends replies only for Telegram-origin messages. Bot credentials are local protected files. No first-user authorization or groups.

## Backend integration

Production enables chat only with `MWSB_CHAT_ENABLED=1`, `MWSB_SUBSCRIPTION_VERIFIED=1` and a dedicated `MWSB_AUTH_DIR`. Telegram polling additionally requires `MWSB_TELEGRAM_ENABLED=1`; test instances never activate external polling by default. The account catalog must also load successfully. Missing optional runtime modules leave phase one available and report chat as unavailable. `POST /api/chat/resume` is an authenticated, CSRF-protected explicit queue resume after reconnecting local authentication or waiting for quota.

`ChatStore(database).enqueue(text, origin, idempotency_key, attachment_ids=[], model=None, external_reply=None)` returns `{job_id,message_id}`. `lookup_idempotency(key, origin='telegram')` returns that result or `None`. `view(conversation_id=None)` includes runtime and Telegram availability, and job `delivery_status`. `ChatWorker(store,controller,runtime,completion_hook=None,attachments_resolver=None)` accepts the synchronous runtime and dispatches it through `asyncio.to_thread`. The optional completion hook receives `(job,content)` only for Telegram jobs; the durable outbox is preferred.

The `chat_outbox` table stores `id`, unique `job_id`, JSON `external_reply`, `content`, `status` and `created_at`. `pending_outbox()` decodes `external_reply`. Before sending, `claim_outbox(id)` atomically changes `pending` to `uncertain`; `mark_outbox_sent(id)` records success. Delivery errors retain uncertainty and are never automatically resent. Both transitions emit delivery events.

Tools use `tools(name,args)->dict`. `brain_read` returns `{path,content,hash,exists}`; `brain_search` returns `{matches:[{path,snippet,hash}],snapshot_id}`. `brain_propose_update` requires `path`, `content`, the read `base_hash` (or `null` for an addition), and `reason`. Proposals remain `proposed` until the turn completes; cancelled, failed and uncertain turns cannot apply them. Eligible proposals become `queued`, then `applying`, and finally `applied` or `rejected`. A full backup precedes the first actual replacement in a batch. Agent-mode startup and scheduled ticks drain durable queued/applying proposals; they never replay inference.

The secret policy is the immutable `brain_policy.py` beside configured `MWSB_SYNC_SCRIPT`, with root-owned paths and no group/world write permissions. It must provide `path_problem(path)` and `content_problem(bytes)`. Missing or invalid policy disables note tools and writes; it does not disable Obsidian. Instruction-file writes, hidden paths, symlinks, deletion requests and stale hashes are rejected. Snapshots stay in private SQLite, and unsafe content is excluded from model reads. Root AGENTS.md, SKILL.md and README.md supply up to 32 KiB of scanned, read-only guidance from the same snapshot. Guidance cannot broaden tools or permissions; archived instructions are not automatically loaded.

Each runtime turn uses a fresh workspace, with image copies under its `.attachments` directory. Workspace copies are removed only after the synchronous runtime has reaped its child. Original attachments remain available through their authenticated IDs. SSE revalidates the session during polling and before each replayed event; logout and expiry terminate existing streams.
