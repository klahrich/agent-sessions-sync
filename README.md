# agent-sessions-sync

Back up your Claude Code and OpenAI Codex sessions every night, and make them searchable so Claude can answer questions about your past conversations.

- **Raw archive in GCS.** `sync-sessions.ps1` rsyncs selected `~/.claude/projects/<project>` folders (session `.jsonl` files, subagent runs, tool results, memory notes) to a Google Cloud Storage bucket, logging in as a service account that can only touch that bucket. Nothing is ever deleted from the bucket, so it keeps sessions that Claude Code has already cleaned up locally.
- **Codex too.** `codex_sync.py` reads `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl[.zst]` and works out each session's project from the working directory it ran in. It copies the raw files to `<bucket>/codex/<project>/YYYY/MM/DD/` and sends markdown copies to Graphon under `<project>/codex/`. It has its own include/exclude lists, and decompresses `.zst` files with the `zstd` CLI.
- **Searchable copy in [Graphon](https://docs.graphon.ai/).** `graphon_sync.py` converts each transcript into a readable markdown conversation (Graphon can't index raw `.jsonl`) and uploads it, plus your memory notes, to an indexed Graphon bucket. Each file is tagged with `project`, `kind`, `session` and `agent` (`claude` or `codex`).
- **A Claude Code skill to query it.** [`skills/graphon-sessions`](skills/graphon-sessions/) teaches Claude to search and question that bucket when you ask things like "what did we decide about auth in myapp?".

Built for Windows (PowerShell + Task Scheduler). The Python parts work anywhere.

## Setup

1. **Config.** Copy `sync-config.example.json` to `sync-config.json` and fill it in. `include`/`exclude` list project short names, which are the folder name under `~/.claude/projects` minus `stripPrefix`, in lowercase. A project on neither list is skipped, and you get a notification so you can decide where it goes.
2. **GCS.**
   - Create a bucket and a service account with `roles/storage.objectAdmin` on that bucket only.
   - Create a key for the service account, then activate it in an isolated gcloud config so your own gcloud login isn't touched:
     ```
     $env:CLOUDSDK_CONFIG = "<cloudsdkConfig from sync-config.json>"
     gcloud auth activate-service-account --key-file <key.json>
     ```
   - If your organization enforces `iam.disableServiceAccountKeyCreation`, override it for that project only, create the key, then delete the override. Keys created while the override was in place keep working.
3. **Graphon.**
   - Put `GRAPHON_API_KEY=gak_...` in `.env`.
   - Create an indexed bucket (`POST https://storage.graphon.sh/v1/workspaces/<ws>/buckets` with `{"name":"claude-sessions","indexing":"enabled","permission":"private"}`). Indexing can't be turned on later.
   - Declare the metadata fields (`PATCH https://api.graphon.ai/v1/workspaces/<ws>/buckets/<id>/metadata-fields` with `project`, `kind`, `session` and `agent` as `text`).
   - Put the workspace and bucket IDs in `sync-config.json`.
4. **Try it.** Run `powershell -File sync-sessions.ps1 -DryRun`, then without `-DryRun`. Run `python graphon_sync.py --dry-run`.
5. **Schedule it.** `powershell -File register-task.ps1` creates a daily 2 AM task. If the laptop was asleep at 2 AM, the task runs at the next opportunity.
6. **Skill.** Link `skills/graphon-sessions` into `~/.claude/skills/` (see [its README](skills/graphon-sessions/README.md)).

Logs are written to `logs/`. Failures and unreviewed projects trigger a Windows notification.

## Things to know

- **Upload key length:** Graphon currently rejects object keys longer than 75 bytes with `404 Unknown storage path`. `graphon_sync.py` keeps keys within that limit.
- **What the markdown keeps:** user messages, Claude's replies, one line per tool call and short previews of tool output. It drops Claude's thinking blocks and images. Increase `CONVERTER_VERSION` after changing the converter to re-upload everything.
- **What's sensitive:** transcripts can contain anything that appeared in a session, including secrets. Keep both buckets private.
