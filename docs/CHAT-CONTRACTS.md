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
