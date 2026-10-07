# MWSecondBrain

Personal self-hosted Obsidian and subscription-backed assistant. Public code only: no real domains, personal notes, production configuration or credentials.

Use `rtk` for shell commands. Backend: Python 3.10+, FastAPI, SQLite. Frontend: TypeScript and Vite. Protect the vault with a single controller; no writes or Git integration while the editor is running. Subscription inference must never fall back to API billing. Do not modify global Codex settings.

Implement focused tests for security, concurrency, data preservation and recovery. Task ownership is set by the orchestrator; no production changes or publishing by subagents.
