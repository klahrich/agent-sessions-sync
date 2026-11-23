# graphon-sessions skill

A Claude Code skill that lets Claude answer questions about your past Claude Code conversations by querying a [Graphon](https://docs.graphon.ai/) bucket. The bucket is filled nightly by `graphon_sync.py` in the root of this repo.

## Install

1. Fill a Graphon bucket with your sessions, e.g. with `graphon_sync.py` from this repo (needs an indexed bucket with `project`, `kind` and `session` metadata fields).
2. Provide credentials, either as environment variables or in a `.env` file at the repo root:
   ```
   GRAPHON_API_KEY=gak_...
   GRAPHON_BUCKET=claude-sessions   # optional, this is the default
   ```
3. Make the skill available to Claude Code globally by linking this folder into `~/.claude/skills/`:
   - Windows (junction, no admin needed):
     `New-Item -ItemType Junction -Path "$HOME\.claude\skills\graphon-sessions" -Target "<repo>\skills\graphon-sessions"`
   - macOS/Linux: `ln -s <repo>/skills/graphon-sessions ~/.claude/skills/graphon-sessions`

The script needs Python 3 and nothing else. Try it directly:

```
python scripts/graphon_query.py projects
python scripts/graphon_query.py answer "what did we decide about auth?" --project myapp
```

## Notes

- **Credentials file:** the script looks for `.env` in the parent folders of its real location, so it finds the repo's `.env` even when it's run through the junction or symlink. Never commit `.env`.
- **Key length limit:** Graphon's storage API currently rejects object keys longer than 75 bytes ("404 Unknown storage path"). `graphon_sync.py` keeps keys within that limit.
