"""
Back up OpenAI Codex sessions: raw files to GCS, readable Markdown to Graphon.

Codex stores sessions by date, not by project:  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl[.zst]
The first record (session_meta) holds the working directory, which is mapped to a project name: the first
folder under ~/Documents (lowercased), e.g. Documents/myapp/dashboard -> "myapp"; Documents/Codex/<date>/... ->
"codex-scratch"; outside Documents -> the folder's own name. include/exclude lists live in sync-config.json "codex".

  GCS:      <bucket>/<gcsPrefix>/<project>/YYYY/MM/DD/<file>    (raw; never deleted, except a stale uncompressed
                                                                 copy once Codex has compressed the session)
  Graphon:  <project>/codex/<session-uuid>.md                   metadata project, kind=session, session, agent=codex

Only new/changed files are sent (codex-state.json remembers size+mtime per file and target).
Compressed sessions are read with the zstd CLI (path in config), so no extra Python packages are needed.

Usage:  python codex_sync.py [--dry-run] [--no-gcs] [--no-graphon] [--dump FILE]
Exit:   0 ok, 1 some uploads failed, 2 config problem.
"""
import argparse, collections, datetime as dt, io, json, os, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # python -I leaves the script dir off sys.path
import graphon_sync as gs   # shared config, Graphon upload, key shortening, logging

CFG, G = gs.CFG, gs.G
C = CFG["codex"]
gs.LOG_FILE = Path(CFG["logDir"]) / f"codex-{dt.date.today():%Y-%m-%d}.log"
log = gs.log
CONVERTER_VERSION = "1"
HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- reading rollouts
def open_rollout(path):
    """Return a text stream over a .jsonl or .jsonl.zst rollout."""
    if path.suffix == ".zst":
        p = subprocess.run([C["zstd"], "-dcq", str(path)], capture_output=True, check=True)
        return io.StringIO(p.stdout.decode("utf-8", "replace"))
    return open(path, encoding="utf-8", errors="replace")


def read_meta(path):
    if path.suffix == ".zst":   # only decompress the start of the file
        with subprocess.Popen([C["zstd"], "-dcq", str(path)], stdout=subprocess.PIPE) as p:
            first = p.stdout.readline(); p.kill()
    else:
        with open(path, "rb") as f:
            first = f.readline()
    d = json.loads(first.decode("utf-8", "replace"))
    return d.get("payload") or {}


def project_of(cwd):
    parts = [p for p in re.split(r"[\\/]+", cwd or "") if p]
    low = [p.lower() for p in parts]
    if "documents" in low:
        i = len(low) - 1 - low[::-1].index("documents")
        rest = parts[i + 1:]
        if not rest:
            return "_documents-root"
        return "codex-scratch" if rest[0].lower() == "codex" else rest[0].lower()
    return low[-1] if low else "_unknown"


# ---------------------------------------------------------------- rollout -> markdown
INJECTED = re.compile(r"<(environment_context|user_instructions|INSTRUCTIONS|permissions instructions|"
                      r"collaboration_mode|skills_instructions|user_shell_command)>.*?</\1>", re.S)


def fix_mojibake(s):
    if "â€" in s or "Ã" in s:
        try:
            return s.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") in ("input_text", "output_text", "text"):
            out.append(b.get("text", ""))
        elif b.get("type") in ("input_image", "image"):
            out.append("[image]")
    return "\n".join(out)


IDE_REQUEST = re.compile(r"^#+\s*My request for Codex:?\s*$", re.M)
IMAGE_TAGS = re.compile(r"</?image\b[^>]*>")


def clean(s):
    s = s or ""
    if s.lstrip().startswith("# Context from my IDE setup"):   # VS Code extension preamble (open tabs etc.)
        m = IDE_REQUEST.search(s)
        s = s[m.end():] if m else ""
    s = fix_mojibake(gs.clean(IMAGE_TAGS.sub("", INJECTED.sub("", s))))
    return "" if s.startswith("# AGENTS.md instructions") else s


def tool_summary(p):
    name = p.get("name") or p.get("type", "").replace("_call", "")
    raw = p.get("arguments") or p.get("input") or p.get("action") or ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if isinstance(raw, dict):
        for k in ("command", "cmd", "path", "file_path", "query", "url", "code"):
            if k in raw:
                v = raw[k]; raw = " ".join(v) if isinstance(v, list) else v; break
    s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    return name, gs.clip(fix_mojibake(s), gs.TOOL_INPUT_MAX).replace("`", "'")


def rollout_to_md(path, project, titles):
    meta, cwd, branch, first_ts, last_ts = {}, None, None, None, None
    out, last_role = [], None

    def turn(role, ts):
        nonlocal last_role
        if role != last_role:
            out.append(f"\n### {role}" + (f" · {gs.fmt_ts(ts)}" if ts else "") + "\n")
            last_role = role

    with open_rollout(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, p, ts = d.get("type"), d.get("payload") or {}, d.get("timestamp")
            if t == "session_meta":
                meta = p; cwd = p.get("cwd"); branch = (p.get("git") or {}).get("branch"); continue
            if t != "response_item" or not isinstance(p, dict):
                continue
            if ts:
                first_ts = first_ts or ts; last_ts = ts
            pt = p.get("type", "")
            if pt == "message" and p.get("role") == "user":
                txt = clean(text_of(p.get("content")))
                if txt:
                    turn("User", ts); out.append(txt + "\n")
            elif pt == "message" and p.get("role") == "assistant":
                txt = clean(text_of(p.get("content")))
                if txt:
                    turn("Codex", ts); out.append(txt + "\n")
            elif pt.endswith("_call"):
                turn("Codex", ts)
                name, s = tool_summary(p)
                out.append(f"- **{name}**: `{s}`\n")
            elif pt.endswith("_call_output"):
                o = p.get("output")
                if isinstance(o, dict):
                    o = o.get("output") or o.get("content") or json.dumps(o)
                res = clean(text_of(o) if isinstance(o, list) else str(o or ""))
                if res:
                    out.append("> result: " + gs.clip(res, gs.TOOL_RESULT_MAX).replace("\n", "\n> ") + "\n")
            # reasoning (encrypted), developer messages, token counts, events: omitted

    sid = meta.get("id") or meta.get("session_id") or path.stem
    title = titles.get(sid) or (out and next((l for l in "".join(out).splitlines() if l and not l.startswith("#")), "")[:80]) or sid
    header = [f"# {title}", "", f"- agent: codex", f"- project: {project}", f"- kind: session", f"- session: {sid}"]
    if first_ts: header.append(f"- dates: {gs.fmt_ts(first_ts)} → {gs.fmt_ts(last_ts)}")
    if cwd: header.append(f"- cwd: {cwd}")
    if branch: header.append(f"- git branch: {branch}")
    if meta.get("source") or meta.get("originator"):
        header.append(f"- client: {meta.get('originator', '')} {meta.get('source', '')}".rstrip())
    return "\n".join(header) + "\n" + "\n".join(out)


# ---------------------------------------------------------------- main
def load_titles():
    titles = {}
    try:
        for line in open(C["sessionIndex"], encoding="utf-8"):
            try:
                d = json.loads(line); titles[d["id"]] = d.get("thread_name") or ""
            except (ValueError, KeyError):
                pass
    except OSError:
        pass
    return titles


def gcloud(*args):
    env = dict(os.environ, CLOUDSDK_CONFIG=CFG["cloudsdkConfig"], CLOUDSDK_CORE_DISABLE_PROMPTS="1")
    p = subprocess.run([CFG["gcloud"], *args, "--no-user-output-enabled"], capture_output=True, text=True, env=env)
    return p.returncode, (p.stderr or p.stdout).strip()[-400:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-gcs", action="store_true")
    ap.add_argument("--no-graphon", action="store_true")
    ap.add_argument("--dump", help="convert one rollout to markdown on stdout and exit")
    a = ap.parse_args()
    titles = load_titles()

    if a.dump:
        sys.stdout.reconfigure(encoding="utf-8")
        p = Path(a.dump); print(rollout_to_md(p, project_of(read_meta(p).get("cwd")), titles)); return 0

    state_path = HERE / C["stateFile"]
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"gcs": {}, "graphon": {}, "meta": {}}
    def save(): state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    include = {p.lower() for p in C["include"]}
    exclude = {p.lower() for p in C["exclude"]}
    root = Path(C["sessionsDir"])
    log(f"=== codex sync start{' (DRY RUN)' if a.dry_run else ''} ===")

    # 1. scan + classify (meta cached by size+mtime so .zst files aren't re-read every night)
    files, unreviewed = [], collections.Counter()
    for f in sorted(list(root.rglob("*.jsonl")) + list(root.rglob("*.jsonl.zst"))):
        st = f.stat(); rel = f.relative_to(root).as_posix(); sig = f"{st.st_size}|{st.st_mtime_ns}"
        cached = state["meta"].get(rel)
        if cached and cached["sig"] == sig:
            project, sid = cached["project"], cached["session"]
        else:
            try:
                m = read_meta(f)
            except Exception as e:
                log(f"FAIL  read {rel}: {e}"); continue
            project, sid = project_of(m.get("cwd")), m.get("id") or m.get("session_id") or f.stem
            state["meta"][rel] = {"sig": sig, "project": project, "session": sid}
        if project in exclude:
            continue
        if project not in include:
            unreviewed[project] += 1; continue
        files.append((f, rel, sig, project, sid))
    if not a.dry_run:
        save()

    failed, sent_gcs, sent_gr = [], 0, 0
    # a session present both as .jsonl and .jsonl.zst: Codex compressed it; only the .zst is current
    compressed = {rel[:-4] for _, rel, *_ in files if rel.endswith(".zst")}
    files = [x for x in files if x[1] not in compressed]

    # 2. GCS: raw files, grouped by destination folder so gcloud starts once per folder
    if not a.no_gcs:
        bydir = collections.defaultdict(list)
        for f, rel, sig, project, sid in files:
            if state["gcs"].get(rel) != sig:
                bydir[f"{CFG['bucket']}/{C['gcsPrefix']}/{project}/{Path(rel).parent.as_posix()}/"].append((f, rel, sig))
        for dest, items in sorted(bydir.items()):
            if a.dry_run:
                for f, rel, _ in items: log(f"would copy  {rel}  ->  {dest}")
                sent_gcs += len(items); continue
            code, err = gcloud("storage", "cp", *[str(f) for f, _, _ in items], dest)
            if code:
                log(f"FAIL  gcs {dest}  {err}"); failed += [r for _, r, _ in items]; continue
            for f, rel, sig in items:
                state["gcs"][rel] = sig; sent_gcs += 1
                stale = rel[:-4] if rel.endswith(".zst") else None
                if stale and stale in state["gcs"]:      # remove the superseded uncompressed copy
                    gcloud("storage", "rm", dest + Path(stale).name); state["gcs"].pop(stale, None)
            log(f"gcs   {len(items)} file(s) -> {dest}")
            save()

    # 3. Graphon: readable markdown per session
    if not a.no_graphon and G.get("enabled"):
        api_key = gs.load_api_key()
        if not api_key:
            log("FAIL  GRAPHON_API_KEY not found"); return 2
        for f, rel, sig, project, sid in files:
            key = gs.fit_key(f"{project}/codex/{sid}.md")
            gsig = f"{CONVERTER_VERSION}|{sig}"
            if state["graphon"].get(key) == gsig:
                continue
            try:
                body = rollout_to_md(f, project, titles).encode("utf-8")
            except Exception as e:
                log(f"FAIL  convert {rel}: {e}"); failed.append(rel); continue
            if a.dry_run:
                log(f"would send  {key}  {len(body)//1024} KB (source {f.stat().st_size//1024} KB)"); sent_gr += 1; continue
            err = gs.put_object(api_key, key, body, "text/markdown",
                                {"project": project, "kind": "session", "session": sid, "agent": "codex"})
            if err:
                log(f"FAIL  {key}  {err}"); failed.append(key)
                if err.startswith(("HTTP 401", "HTTP 403")): break
            else:
                log(f"sent  {key}  {len(body)//1024} KB (source {f.stat().st_size//1024} KB)")
                state["graphon"][key] = gsig; sent_gr += 1; save()

    if unreviewed:
        log("UNREVIEWED codex projects (add to codex include/exclude in sync-config.json): "
            + ", ".join(f"{p} ({n})" for p, n in unreviewed.most_common()))
    log(f"=== codex done: {len(files)} sessions in scope, {sent_gcs} to GCS, {sent_gr} to Graphon, "
        f"{len(failed)} failed, {len(unreviewed)} unreviewed projects ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
