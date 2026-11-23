"""
Upload Claude Code sessions + memory to a Graphon bucket (indexed for AI search).

Graphon can't index raw .jsonl, and raw transcripts are mostly JSON plumbing anyway, so each
transcript is converted to a readable Markdown conversation before upload. The raw .jsonl files
are archived separately in GCS by sync-sessions.ps1.

Per included project (same include list as sync-config.json):
  <folder>/<session>.jsonl                      -> <project>/<session>.md                       kind=session
  <folder>/<session>/subagents/<agent>.jsonl    -> <project>/<session>/subagents/<agent>.md     kind=subagent
  <folder>/memory/*.md                          -> <project>/memory/<file>.md                   kind=memory
Metadata on every object: project=<project>, kind=..., session=<id> (transcripts).

Only new/changed source files are sent (graphon-state.json remembers size+mtime of the last upload).
Never deletes anything in Graphon. Standard library only.

Usage:  python graphon_sync.py [--dry-run] [--only PROJECT] [--dump FILE.jsonl]
Exit:   0 ok, 1 some uploads failed, 2 config/auth problem.
"""
import argparse, base64, datetime as dt, json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path

CONVERTER_VERSION = "2"          # bump to force re-upload of every transcript after changing the converter (2: agent=claude metadata)
TOOL_INPUT_MAX = 400
TOOL_RESULT_MAX = 600

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "sync-config.json").read_text(encoding="utf-8"))
G = CFG["graphon"]
LOG_DIR = Path(CFG["logDir"]); LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"graphon-{dt.date.today():%Y-%m-%d}.log"


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


# ---------------------------------------------------------------- transcript -> markdown
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NOISE_TAGS = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|local-command-caveat|command-message)>.*?</\1>",
    re.S)


def clean(text):
    text = ANSI.sub("", text or "")
    text = NOISE_TAGS.sub("", text)
    m = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if m:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        text = f"{m.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    return text.strip()


def clip(text, n):
    text = text.strip()
    return text if len(text) <= n else text[:n].rstrip() + f" … [+{len(text) - n} chars]"


def summarize_tool_input(name, inp):
    if not isinstance(inp, dict):
        return clip(str(inp), TOOL_INPUT_MAX)
    for k in ("command", "file_path", "pattern", "url", "query", "description", "prompt"):
        if k in inp and isinstance(inp[k], str):
            extra = f" ({inp['description']})" if k == "command" and inp.get("description") else ""
            return clip(inp[k], TOOL_INPUT_MAX) + extra
    return clip(json.dumps(inp, ensure_ascii=False), TOOL_INPUT_MAX)


def tool_result_text(block):
    c = block.get("content")
    if isinstance(c, list):
        parts = [b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')}]" for b in c if isinstance(b, dict)]
        c = "\n".join(parts)
    return clean(str(c or ""))


def fmt_ts(ts):
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def transcript_to_md(path, project, kind):
    title, cwd, branch, first_ts, last_ts = None, None, None, None, None
    out, last_role = [], None

    def turn(role, ts):
        nonlocal last_role
        if role != last_role:
            out.append(f"\n### {role}" + (f" · {fmt_ts(ts)}" if ts else "") + "\n")
            last_role = role

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "ai-title" and d.get("aiTitle"):
                title = d["aiTitle"]
            if t not in ("user", "assistant") or d.get("isMeta"):
                continue
            ts = d.get("timestamp")
            if ts:
                first_ts = first_ts or ts; last_ts = ts
            cwd = cwd or d.get("cwd"); branch = branch or d.get("gitBranch")
            content = (d.get("message") or {}).get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])

            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if t == "user" and bt == "text":
                    txt = clean(b.get("text"))
                    if txt:
                        turn("User", ts); out.append(txt + "\n")
                elif t == "user" and bt == "image":
                    turn("User", ts); out.append("[image]\n")
                elif t == "user" and bt == "tool_result":
                    res = tool_result_text(b)
                    if res:
                        tag = "error" if b.get("is_error") else "result"
                        out.append(f"> {tag}: " + clip(res, TOOL_RESULT_MAX).replace("\n", "\n> ") + "\n")
                elif t == "assistant" and bt == "text":
                    txt = clean(b.get("text"))
                    if txt:
                        turn("Claude", ts); out.append(txt + "\n")
                elif t == "assistant" and bt == "tool_use":
                    turn("Claude", ts)
                    out.append(f"- **{b.get('name')}**: `{summarize_tool_input(b.get('name'), b.get('input')).replace('`', "'")}`\n")
                # thinking blocks are intentionally omitted

    header = [f"# {title or path.stem}", "",
              f"- project: {project}", f"- kind: {kind}", f"- session: {path.stem}"]
    if first_ts: header.append(f"- dates: {fmt_ts(first_ts)} → {fmt_ts(last_ts)}")
    if cwd: header.append(f"- cwd: {cwd}")
    if branch: header.append(f"- git branch: {branch}")
    return "\n".join(header) + "\n" + "\n".join(out)


# ---------------------------------------------------------------- graphon api
def load_api_key():
    env = HERE / G["envFile"]
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*GRAPHON_API_KEY\s*=\s*"?([^"\s]+)"?\s*$', line)
            if m:
                return m.group(1)
    return os.environ.get("GRAPHON_API_KEY")


def key_token(key):
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def ascii_meta(v):
    return "".join(ch for ch in str(v) if 32 <= ord(ch) < 127)[:200]


def put_object(api_key, key, body, content_type, meta):
    url = f"{G['storageOrigin']}/v1/workspaces/{G['workspaceId']}/buckets/{G['bucketId']}/objects/{key_token(key)}/content"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": content_type}
    headers.update({f"x-amz-meta-{k}": ascii_meta(v) for k, v in meta.items()})
    err = None
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                r.read(); return None
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code} {e.read()[:300].decode('utf-8', 'replace')}"
            if e.code in (400, 401, 403, 404, 411, 412, 413):
                return err
        except Exception as e:  # network
            err = f"{type(e).__name__}: {e}"
        time.sleep(5 * attempt)
    return err


# ---------------------------------------------------------------- main
def short_name(folder):
    prefix = CFG["stripPrefix"]
    if folder.lower() == prefix.rstrip("-").lower():
        return "_documents-root"
    if folder.lower().startswith(prefix.lower()):
        return folder[len(prefix):].lower()
    return folder.lower()


# Graphon's storage router rejects object keys whose base64url token is > 100 chars (key > 75 bytes)
# with "404 Unknown storage path", despite the documented 1024-char limit. Keep keys short.
MAX_KEY_BYTES = 75


def fit_key(key):
    if len(key.encode()) <= MAX_KEY_BYTES:
        return key
    import hashlib
    folder, _, name = key.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    h = hashlib.sha1(key.encode()).hexdigest()[:6]
    room = MAX_KEY_BYTES - len(folder.encode()) - 1 - len(h) - 2 - len(ext)
    if room < 4:
        raise ValueError(f"key too long even after shortening: {key}")
    return f"{folder}/{stem[:room]}-{h}.{ext}"


def collect(project_dir, project):
    for f in sorted(project_dir.glob("*.jsonl")):
        yield f, fit_key(f"{project}/{f.stem}.md"), "session", {"project": project, "kind": "session", "session": f.stem, "agent": "claude"}
    for f in sorted(project_dir.glob("*/subagents/*.jsonl")):
        sid = f.parent.parent.name
        yield f, fit_key(f"{project}/subagents/{f.stem}.md"), "subagent", {"project": project, "kind": "subagent", "session": sid, "agent": "claude"}
    for f in sorted((project_dir / "memory").glob("*.md")):
        yield f, fit_key(f"{project}/memory/{f.name}"), "memory", {"project": project, "kind": "memory", "agent": "claude"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="limit to one project short name")
    ap.add_argument("--dump", help="convert one .jsonl to markdown on stdout and exit")
    a = ap.parse_args()

    if a.dump:
        sys.stdout.reconfigure(encoding="utf-8")
        print(transcript_to_md(Path(a.dump), "?", "session")); return 0

    api_key = load_api_key()
    if not api_key:
        log("FAIL  GRAPHON_API_KEY not found"); return 2

    state_path = HERE / G["stateFile"]
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    include = {p.lower() for p in CFG["include"]}

    log(f"=== graphon sync start{' (DRY RUN)' if a.dry_run else ''} -> bucket {G['bucketName']} ===")
    sent = skipped = 0; failed = []
    for d in sorted(Path(CFG["projectsDir"]).iterdir()):
        if not d.is_dir():
            continue
        project = short_name(d.name)
        if project not in include or (a.only and project != a.only):
            continue
        for src, key, kind, meta in collect(d, project):
            st = src.stat()
            sig = f"{CONVERTER_VERSION if kind != 'memory' else 'm2'}|{st.st_size}|{st.st_mtime_ns}"
            if state.get(key) == sig:
                skipped += 1; continue
            try:
                if kind == "memory":
                    body, ctype = src.read_bytes(), "text/markdown"
                else:
                    body, ctype = transcript_to_md(src, project, kind).encode("utf-8"), "text/markdown"
            except Exception as e:
                log(f"FAIL  {key}  convert: {e}"); failed.append(key); continue
            size = f"{len(body) // 1024} KB (source {st.st_size // 1024} KB)"
            if a.dry_run:
                log(f"would send  {key}  {size}"); sent += 1; continue
            err = put_object(api_key, key, body, ctype, meta)
            if err:
                log(f"FAIL  {key}  {err}"); failed.append(key)
                if err.startswith(("HTTP 401", "HTTP 403")):
                    log("auth problem; stopping"); break
            else:
                log(f"sent  {key}  {size}"); sent += 1
                state[key] = sig
                state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        else:
            continue
        break

    log(f"=== graphon done: {sent} sent, {skipped} unchanged, {len(failed)} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
