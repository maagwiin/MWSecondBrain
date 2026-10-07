# MWSecondBrain

Self-hosted personal vault with authenticated browser access to Obsidian, conservative Git synchronization, local recovery snapshots and a shared web/Telegram assistant.

The application includes persistent chat, controlled note updates and attachments. Enabling chat requires a successful real inference using the user's ChatGPT subscription through the official open-source Sign in with ChatGPT flow. Paid API keys and billing fallback are outside this project's design. Telegram requires separate explicit enablement and a paired numeric user ID.

## Components

- FastAPI, SQLite and an exclusive controller coordinate the vault.
- TypeScript/Vite provides the Portuguese dashboard.
- A pinned LinuxServer Obsidian container provides editing, native search and graph.
- Caddy authenticates Obsidian requests, including WebSocket handshakes.
- A narrowly scoped root helper starts and gracefully stops the editor. The application has no Docker socket access.
- Codex App Server runs in a confined process with only controlled vault tools; quota or authentication failures pause the queue.
- Web and Telegram share one durable conversation and process one turn at a time. Attachments stay outside Git.

Editing and synchronization are mutually exclusive. Closing the browser tab does not release editing. Use the dashboard to close Obsidian, preserve a recovery snapshot and synchronize. Uncertain process state blocks writes.

## Development

Use Python 3.10 or later and Node.js 22 or later. Create a virtual environment, install the package with its test extras, then run:

```sh
python -m pytest -q
npm ci --prefix frontend
npm run build --prefix frontend
npm test --prefix frontend
```

Browser tests require Playwright Chromium and its system dependencies. On small servers, run builds and browser tests sequentially.

## Deployment and recovery

Review the files in `deploy/` before installing. The deployment assumes Linux with systemd, Docker, Caddy and a dedicated Git credential. The sync adapter requires a reviewed private synchronizer implementing the documented contract. Never put a vault, credentials or generated state in this repository.

Configuration examples use fictitious domains. Initialize the password locally with `mwsb init-password`; there is no default password or public registration. Git credentials and OAuth registration are separate from the login password.

See [HTTP and controller contracts](docs/CONTRACTS.md), [chat contracts](docs/CHAT-CONTRACTS.md), [subscription setup](docs/CODEX.md) and [backup and isolated restore](docs/BACKUP.md). Local snapshots protect against operational mistakes, but do not protect against losing the server or disk. After restoration, reconnect credentials separately.

MIT licensed. Obsidian and the container distribution retain their own licenses.
