# Codex runtime

Gepeto uses the selected account's Sign in with ChatGPT authorization through Codex App Server. `CodexRuntime` never uses global Codex credentials, an API key, an alternate provider or automatic inference retries. The controller owns the queue, conversation history, snapshots, attachments and note changes.

## Interface

```python
runtime = CodexRuntime(auth_dir, work_dir)
models = runtime.models()  # [{id, label, supports_images}]
answer = runtime.run(messages, model, tools, on_delta, cancelled, images=[])
```

Calls are synchronous. The worker runs them in its thread and serializes jobs. `messages` contains `user` and `assistant` text and ends with a user message. A null model selects the account catalog's default or first visible model. Image support comes only from explicit catalog metadata. An unsupported or unknown model fails before the process starts.

`tools(name, arguments)` returns JSON-compatible data. Only `brain_search`, `brain_read` and `brain_propose_update` in the `brain` namespace are allowed. Arguments are validated before dispatch. Repeated call IDs reuse their result; a changed payload with the same ID fails. The callback must enforce vault paths, capture permission, operation deduplication and controller locks. A proposal never directly changes a note.

The worker sets `runtime.brain_guidance` from the same consistent snapshot used for the turn. The runtime accepts at most 32 KiB of UTF-8 text, labels it subordinate data and places immutable rules before it. Guidance may refine queries, capture criteria, citations and privacy, but cannot change tools, permissions, capture pause or controller decisions. It never enables shell or executes a skill. Only the controller selects which root guidance files to load; archived skills are not preloaded by the runtime.

Fixed instructions require consulting relevant indices and notes, citing exact relative note paths and separating facts from inference. Capture proposes useful durable syntheses with provenance rather than full transcripts. Pause and explicit “não guarde” forbid capture. Deletions and instruction-file edits are forbidden. Conflicting facts must be shown with their sources and referred to the user before replacement. These model instructions supplement the controller's enforced tool and write restrictions.

Create a fresh runtime with the same `auth_dir` and a new snapshot directory for each turn. The constructor creates a missing snapshot directory with mode 0700. The controller populates a consistent snapshot before `run`. Approved PNG, JPEG and WebP files must be inside `work_dir/.attachments`, without symlinks, and at most 10 MiB each. They are remapped to `/workspace/.attachments` in the child process.

## OAuth lifecycle

Use the [account login helper](../tools/account-login/README.md) for initial authorization and protected import. The authorization directory belongs to the service user and has mode 0700; `credentials.json` and `.refresh.lock` have mode 0600. Credentials must be a regular file owned by the runtime user. Symlinks and insecure modes are rejected. OAuth and snapshot directories must be separate, and OAuth cannot live under the system directories mounted in the child.

Before catalog access, an exclusive `flock` protects the registration. If the token expires within 60 seconds, the runtime invokes the internal Node refresh helper while holding that lock. Concurrent callers then read the rotated credentials. Refresh preserves the client registration and subject, checks the plan scopes and validates any new ID token against official JWKS. Credentials are replaced atomically only after validation. Revocation requests a new login; quota pauses processing. The helper's stdout contains only a result classification, never tokens.

Node.js 22+, Codex and bubblewrap are required on Linux. Codex must be installed under `/usr`. Deploy the entire release, including `tools/account-login`, with its virtual environment at `RELEASE/.venv`; a standalone Python wheel does not contain the JavaScript helpers. Source execution resolves helpers from the repository; an installed release resolves them next to its `.venv`.

## Whole-process confinement

Bubblewrap confines the entire App Server, including direct server file reads. It exposes the snapshot read-only at `/workspace`, a dedicated writable cache at `/runtime`, public system libraries and programs read-only, required TLS/DNS files, namespaced `/proc`, `/dev` and temporary `/tmp`. Credentials, personal home directories, the rest of the vault and other server state are not mounted. The child receives a fresh OAuth token only through its private environment and uses `CODEX_HOME=/runtime`. The runtime does not modify global Codex configuration or install hooks.

The child retains network connectivity for the configured inference endpoint. Shell, unified exec, patch, local image viewing, web search, apps, plugins, browsers, computer use, image generation, code mode, hooks, skills, goals, user-input tools and subagents are disabled. Only the controlled `brain` namespace is advertised in the tested Codex 0.160.1 request. Unexpected native tool requests or native execution events terminate the runtime. Read-only mounts also prevent direct writes if a future version changes its tool surface.

The compatible Codex 0.160.1 turn policy is `{type: "readOnly", networkAccess: false}`. Its removed `readOnly.access` field is never used. That policy alone does not restrict all server reads; the operating-system mounts provide that boundary. If bubblewrap is unavailable or namespaces are denied, the runtime fails closed. Run the service as its dedicated user, never root, and validate confinement under the actual service hardening settings before promotion.

## Errors and cancellation

`AuthRequired`, `QuotaExceeded`, `RuntimeFailure` and `Cancelled` have generic public messages. Remote response bodies and tool callback exceptions are not exposed. A quota error does not retry or switch providers. RPCs time out after 20 seconds; a turn after 300 seconds. Cancellation requests `turn/interrupt`, then reaps the process group with TERM/KILL. Only a completed turn returns a final answer; progressive deltas can precede an error and the worker must preserve its failed status.

## Verification

```bash
rtk .venv/bin/python -m pytest tests/test_codex.py -q
rtk npm --prefix tools/account-login test
```

Protocol tests use real pipes with a simulated peer. Refresh tests use synthetic credentials and simulated OAuth responses. They cover concurrency, unsafe credentials, revoked sessions, quota, tool restrictions, streamed deltas, cancellation, model selection and attachment boundaries.

Optional host checks require user namespaces. Run outside a managed sandbox that denies UID mappings:

```bash
rtk bash -c 'MWSB_TEST_CONFINEMENT=1 MWSB_TEST_APP_SERVER=1 .venv/bin/python -m pytest tests/test_codex.py -q'
```

These checks use fictitious files and tokens. They verify external-file isolation, read-only writes, real App Server startup and the tool list sent to a local fake HTTP provider. They never contact OpenAI for inference. Account access and subscription entitlement require the separate authorized acceptance gate; neither startup nor mock checks establish those facts.
