"""
Query past Claude Code sessions stored in a Graphon bucket (see graphon_sync.py in this repo).

  python graphon_query.py search  "query" [--project P] [--agent claude|codex] [--kind session|subagent|memory] [--keyword] [--limit N] [--context N]
  python graphon_query.py answer  "question" [--project P] [--agent claude|codex] [--kind K] [--instructions "..."]
  python graphon_query.py get     KEY [--start N] [--end N]
  python graphon_query.py list    [--project P] [--limit N]
  python graphon_query.py projects

Config (env vars, or a .env file found by walking up from this script's real location, or GRAPHON_ENV_FILE):
  GRAPHON_API_KEY   required
  GRAPHON_BUCKET    bucket name, default "claude-sessions"
The workspace id is read from the key itself (GET /v1/me). Standard library only.
"""
import argparse, base64, json, os, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

API = "https://api.graphon.ai/v1"
STORAGE = "https://storage.graphon.sh/v1"


def load_env():
    cands = [os.environ.get("GRAPHON_ENV_FILE")] if os.environ.get("GRAPHON_ENV_FILE") else []
    here = Path(__file__).resolve()
    cands += [p / ".env" for p in here.parents]
    for c in cands:
        try:
            for line in Path(c).read_text(encoding="utf-8").splitlines():
                k, sep, v = line.partition("=")
                if sep and k.strip().startswith("GRAPHON_") and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")
            if os.environ.get("GRAPHON_API_KEY"):
                return
        except OSError:
            continue


def call(method, url, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {os.environ['GRAPHON_API_KEY']}",
        **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            b = r.read()
            return b if raw else json.loads(b or b"{}")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:600]
        sys.exit(f"Graphon HTTP {e.code}: {msg}")


def tok(key):
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def ctx():
    ws = call("GET", f"{API}/me")["workspaceId"]
    name = os.environ.get("GRAPHON_BUCKET", "claude-sessions")
    items = call("GET", f"{STORAGE}/workspaces/{ws}/buckets?name={urllib.parse.quote(name)}").get("items", [])
    if not items:
        sys.exit(f"No Graphon bucket named {name!r} in workspace {ws}")
    return ws, items[0]["id"]


def scope(a):
    s = {}
    clauses = []
    if getattr(a, "project", None): clauses.append({"field": "project", "op": "eq", "value": a.project.lower()})
    if getattr(a, "kind", None): clauses.append({"field": "kind", "op": "eq", "value": a.kind})
    if getattr(a, "agent", None): clauses.append({"field": "agent", "op": "eq", "value": a.agent})
    if clauses: s["filter"] = {"and": clauses}
    return s


_cache = {}
def content(ws, bkt, key):
    if key not in _cache:
        _cache[key] = call("GET", f"{STORAGE}/workspaces/{ws}/buckets/{bkt}/objects/{tok(key)}/content", raw=True).decode("utf-8", "replace")
    return _cache[key]


def header_of(text):
    lines = text.splitlines()[:8]
    title = lines[0].lstrip("# ").strip() if lines else ""
    dates = next((l[len("- dates: "):] for l in lines if l.startswith("- dates: ")), "")
    return title, dates


def cmd_search(a):
    ws, bkt = ctx()
    body = {"target": bkt, "query": a.query, "mode": "keyword" if a.keyword else "hybrid",
            "resultType": "object", "limit": a.limit, **scope(a)}
    res = call("POST", f"{API}/workspaces/{ws}/search", body)
    for w in res.get("warnings") or []: print(f"(warning: {w})")
    results = res.get("results") or []
    if not results:
        print("No results."); return
    for r in results:
        text = content(ws, bkt, r["key"])
        title, dates = header_of(text)
        print(f"\n=== #{r['rank']}  {r['key']}  (score {r['score']:.4f})")
        if title: print(f"    title: {title}" + (f"   dates: {dates}" if dates else ""))
        for sp in r.get("spans", [])[:3]:
            if sp.get("unit") != "chars": continue
            s, e = max(0, sp["start"] - a.context), min(len(text), sp["end"] + a.context)
            snippet = text[s:e].strip()
            if len(snippet) > a.max_chars: snippet = snippet[:a.max_chars] + " …"
            print(f"  --- chars {sp['start']}-{sp['end']}\n" + "\n".join("    " + l for l in snippet.splitlines()))


def cmd_answer(a):
    ws, bkt = ctx()
    body = {"target": bkt, "query": a.query, "sources": "cited", **scope(a)}
    if a.instructions: body["instructions"] = a.instructions
    res = call("POST", f"{API}/workspaces/{ws}/answer", body)
    print(res.get("answer") or json.dumps(res, indent=1)[:4000])
    srcs = res.get("sources") or []
    if srcs:
        print("\nSources:")
        for s in srcs:
            sp = s.get("span") or {}
            rng = f"  --start {sp['start']} --end {sp['end']}" if sp.get("unit") == "chars" else ""
            print(f"  [[{s.get('ref', '')}]] {s.get('key')}{rng}")


def cmd_get(a):
    ws, bkt = ctx()
    text = content(ws, bkt, a.key)
    sys.stdout.write(text[a.start:a.end] if (a.start or a.end) else text)


def cmd_list(a):
    ws, bkt = ctx()
    q = {"limit": a.limit, "sort": "newest"}
    if a.project: q["prefix"] = a.project.lower() + "/"
    res = call("GET", f"{STORAGE}/workspaces/{ws}/buckets/{bkt}/objects?{urllib.parse.urlencode(q)}")
    for o in res.get("items") or []:
        print(f"{o.get('createdAt', '')[:16]}  {int(o.get('sizeBytes', 0)) // 1024:>6} KB  {o['key']}")


def cmd_projects(a):
    ws, bkt = ctx()
    cursor = None
    while True:
        q = {"delimiter": "/", "limit": 1000, **({"cursor": cursor} if cursor else {})}
        res = call("GET", f"{STORAGE}/workspaces/{ws}/buckets/{bkt}/objects?{urllib.parse.urlencode(q)}")
        for it in res.get("items") or []:
            if it.get("kind") == "prefix":
                print(it["prefix"].rstrip("/"))
        cursor = res.get("nextCursor")
        if not cursor:
            break


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    if not os.environ.get("GRAPHON_API_KEY"):
        sys.exit("GRAPHON_API_KEY not set (env var or .env)")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--project"); s.add_argument("--kind"); s.add_argument("--agent", choices=["claude", "codex"])
    s.add_argument("--keyword", action="store_true"); s.add_argument("--limit", type=int, default=5)
    s.add_argument("--context", type=int, default=300); s.add_argument("--max-chars", type=int, default=2500)
    s = sub.add_parser("answer"); s.add_argument("query"); s.add_argument("--project"); s.add_argument("--kind"); s.add_argument("--agent", choices=["claude", "codex"])
    s.add_argument("--instructions")
    s = sub.add_parser("get"); s.add_argument("key"); s.add_argument("--start", type=int); s.add_argument("--end", type=int)
    s = sub.add_parser("list"); s.add_argument("--project"); s.add_argument("--limit", type=int, default=50)
    sub.add_parser("projects")
    a = ap.parse_args()
    {"search": cmd_search, "answer": cmd_answer, "get": cmd_get, "list": cmd_list, "projects": cmd_projects}[a.cmd](a)


if __name__ == "__main__":
    main()
