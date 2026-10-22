#!/usr/bin/env python3
"""skill-guard: every agent skill or plugin must pass a SkillSpector scan
before it is installed or used. The procedure is in README.md.

  setup                            install the pinned scanner tools (uv)
  install-hooks                    add the hooks to ~/.claude/settings.json
  install-plugins                  install the plugins listed in plugins.json
  scan SOURCE [--skill NAME]...    scan before installing; SOURCE is a local
       [--label TEXT]...           path, a git URL or a GitHub owner/repo
  check                            scan installed skills and plugins that are
                                   not in approved.json
  approve PATH --note TEXT         record an installed skill or plugin that
                                   was reviewed and that the user approved
  session-start                    SessionStart hook: setup, then check
  gate                             PreToolUse hook for Bash: stop installs of
                                   skills or plugins that were not scanned

Uses only the standard library, so it runs with plain `python3 -I`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
POLICY = "the skill-guard README (github.com/shlomichai44/claude-skill-guard)"
CLI = "skill-guard"
APPROVED_FILE = HERE / "approved.json"
PLUGINS_FILE = HERE / "plugins.json"
NVIDIA_ROOT_CERT = HERE / "trust" / "nvidia-agent-root.pem"
STATE_DIR = Path(os.environ.get("SKILL_GUARD_STATE") or Path.home() / ".cache" / "skill-guard")
SCANS_FILE = STATE_DIR / "scans.json"
CACHE_FILE = STATE_DIR / "results-cache.json"

# Pinned to the commit checked on 2026-10-22 at github.com/NVIDIA/skillspector
# (GitHub org verified for nvidia.com). Update both lines together.
SKILLSPECTOR_SPEC = (
    "skillspector @ git+https://github.com/NVIDIA/skillspector.git"
    "@d7338f58200a9ae2a24810cee08911ab68b107a7"
)
SKILLSPECTOR_VERSION = "2.12.0"
MODEL_SIGNING_SPEC = "model-signing==1.1.1"

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}
MAX_UNITS_WITHOUT_SKILL = 30
OK_STATUSES = {"approved", "pass", "pass-known"}
HIGH = {"HIGH", "CRITICAL"}
# A file the skill names but does not bundle (often one it writes at run time).
BENIGN_GAPS = {"reference_missing"}
CACHE_SCHEMA = 2


class GuardError(Exception):
    pass


# ----------------------------------------------------------------- helpers

def tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / name
    return str(local) if local.exists() else None


def tail(text: str, lines: int = 4) -> str:
    return " | ".join((text or "").strip().splitlines()[-lines:])


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def today() -> str:
    return now().date().isoformat()


# ------------------------------------------------------------------- setup

def setup() -> list[str]:
    """Install the scanner tools when they are missing. Returns problems."""
    problems: list[str] = []
    uv = tool("uv")

    def run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=900, check=False)

    sk = tool("skillspector")
    ready = False
    if sk:
        ready = SKILLSPECTOR_VERSION in (run([sk, "--version"]).stdout or "")
    if not ready:
        if not uv:
            problems.append("uv is missing, so SkillSpector could not be installed")
        else:
            r = run([uv, "tool", "install", "--force", SKILLSPECTOR_SPEC])
            if r.returncode != 0:
                problems.append("SkillSpector install failed: " + tail(r.stderr))

    if not tool("model_signing"):
        if not uv:
            problems.append("uv is missing, so model-signing could not be installed")
        else:
            r = run([uv, "tool", "install", "--force", MODEL_SIGNING_SPEC, "--with", "pyopenssl"])
            if r.returncode != 0:
                problems.append("model-signing install failed: " + tail(r.stderr))
    return problems


# ------------------------------------------------------------------- units

def is_plugin(p: Path) -> bool:
    return (p / ".claude-plugin" / "plugin.json").is_file()


def is_skill(p: Path) -> bool:
    return (p / "SKILL.md").is_file()


def unit_name(p: Path) -> str:
    if is_plugin(p):
        data = load_json(p / ".claude-plugin" / "plugin.json", {})
        return str(data.get("name") or p.name)
    try:
        text = (p / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return p.name
    if text.startswith("---"):
        front = text.split("---", 2)[1] if text.count("---") >= 2 else ""
        m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", front, re.MULTILINE)
        if m:
            return m.group(1).strip()
    return p.name


def make_unit(p: Path) -> dict:
    return {
        "name": unit_name(p),
        "dir": p.name,
        "kind": "plugin" if is_plugin(p) else "skill",
        "path": str(p),
    }


def find_units(root: Path, exclude: tuple[str, ...] = ()) -> list[dict]:
    """A unit is a plugin (with every file in it) or a skill outside a plugin."""
    if not root.is_dir():
        return []
    if is_plugin(root) or is_skill(root):
        return [make_unit(root)]
    units = []
    for cur, dirs, _files in os.walk(root):
        keep = []
        for d in sorted(dirs):
            if d in SKIP_DIRS or d in exclude:
                continue
            p = Path(cur) / d
            if is_plugin(p) or is_skill(p):
                units.append(make_unit(p))
            else:
                keep.append(d)
        dirs[:] = keep
    return units


def dir_hash(path: Path) -> str:
    h = hashlib.sha256()
    files = []
    for cur, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        files.extend(Path(cur) / n for n in names)
    for f in sorted(files):
        h.update(f.relative_to(path).as_posix().encode() + b"\0")
        if f.is_symlink():
            h.update(b"link:" + os.readlink(f).encode())
        else:
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


# ---------------------------------------------------------------- scanning

def run_skillspector(path: Path) -> dict:
    sk = tool("skillspector")
    if not sk:
        raise GuardError(
            f"SkillSpector is not installed: run `{CLI} setup`"
        )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=STATE_DIR) as tmp:
        out = Path(tmp) / "report.json"
        args = [sk, "scan", str(path), "--no-llm", "--format", "json", "--output", str(out)]
        r = subprocess.run(args, capture_output=True, text=True, timeout=900, cwd=tmp, check=False)
        if not out.exists():
            raise GuardError(f"SkillSpector failed on {path}: {tail(r.stderr or r.stdout)}")
        return json.loads(out.read_text(encoding="utf-8"))


def summarize(report: dict) -> dict:
    risk = report.get("risk_assessment") or {}
    issues = []
    for i in report.get("issues") or []:
        loc = i.get("location") or {}
        issues.append({
            "id": i.get("id"),
            "severity": i.get("severity"),
            "pattern": i.get("pattern"),
            "where": f"{loc.get('file')}:{loc.get('start_line')}",
            "finding": " ".join(str(i.get("finding") or "").split())[:160],
            "fingerprint": i.get("match_fingerprint")
            or f"{i.get('id')}:{loc.get('file')}:{i.get('finding')}",
        })
    completeness = report.get("analysis_completeness") or {}
    gaps = [
        {
            "reason": g.get("reason_code"),
            "fatal": bool(g.get("fatal")),
            "where": f"{g.get('path')}:{g.get('start_line')}",
        }
        for g in completeness.get("ledger_exceptions") or []
    ]
    return {
        "score": int(risk.get("score") or 0),
        "recommendation": str(risk.get("recommendation") or "CAUTION"),
        "complete": bool(completeness.get("is_complete")),
        "coverage": completeness.get("coverage_percent"),
        "gaps": gaps,
        "issues": issues,
    }


def verify_signature(ms: str, skill_dir: Path, sig: Path, strict: bool) -> str:
    args = [ms, "verify", "certificate", str(skill_dir), "--signature", str(sig),
            "--certificate_chain", str(NVIDIA_ROOT_CERT)]
    if not strict:
        args.append("--ignore_unsigned_files")
    r = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0 and "Verification succeeded" in out:
        return "ok"
    if "No module named" in out:
        return "tool-broken"
    return "failed"


def check_signatures(path: Path) -> str:
    """NVIDIA signs each published skill with a detached skill.oms.sig."""
    sigs = [s for s in path.rglob("skill.oms.sig") if ".git" not in s.parts]
    if not sigs:
        return "none"
    ms = tool("model_signing")
    if not ms or not NVIDIA_ROOT_CERT.is_file():
        return "not-checked"
    results = []
    for sig in sigs:
        res = verify_signature(ms, sig.parent, sig, strict=True)
        if res == "failed" and verify_signature(ms, sig.parent, sig, strict=False) == "ok":
            res = "extra-files"
        results.append(res)
    for worst in ("failed", "tool-broken", "extra-files"):
        if worst in results:
            return {"tool-broken": "not-checked"}.get(worst, worst)
    return "verified"


def evaluate(unit: dict, use_cache: bool = True) -> dict:
    """Scan a unit (or reuse the cached scan of identical content)."""
    path = Path(unit["path"])
    sha = dir_hash(path)
    cache = load_json(CACHE_FILE, {}) if use_cache else {}
    key = f"{CACHE_SCHEMA}:{SKILLSPECTOR_VERSION}:{sha}"
    if key in cache:
        result = cache[key]
    else:
        result = summarize(run_skillspector(path))
        result["signature"] = check_signatures(path)
        cache = load_json(CACHE_FILE, {})
        cache[key] = result
        save_json(CACHE_FILE, cache)
    return {**unit, "sha256": sha, **result}


def approved_entries() -> list[dict]:
    return load_json(APPROVED_FILE, {}).get("entries", [])


def decide(res: dict, entries: list[dict]) -> tuple[str, str]:
    """Return (status, reason). Statuses: approved, pass, pass-known, review, blocked."""
    same = [e for e in entries if e.get("name") == res["name"] and e.get("kind") == res["kind"]]
    if any(e.get("sha256") == res["sha256"] for e in same):
        return "approved", "same content the user approved"
    if res["signature"] == "failed":
        return "blocked", "NVIDIA signature does not verify: the files were changed after signing"
    high = [i for i in res["issues"] if i["severity"] in HIGH]
    clean = not high and res["signature"] != "extra-files"
    if clean and res["recommendation"] == "SAFE":
        return "pass", "SkillSpector: SAFE"
    gaps = res.get("gaps") or []
    if clean and res["score"] < 21 and gaps and all(g["reason"] in BENIGN_GAPS and not g["fatal"] for g in gaps):
        return "pass", "SkillSpector: low score; only names files it does not bundle"
    found = {i["fingerprint"] for i in res["issues"]}
    for e in same:
        if found <= set(e.get("accepted_findings") or []):
            return "pass-known", f"changed, but only findings the user accepted on {e.get('approved_on')}"
    if res["signature"] == "extra-files":
        return "review", "NVIDIA signature covers the signed files, but there are extra unsigned files"
    if res["recommendation"] == "DO_NOT_INSTALL":
        return "blocked", f"SkillSpector: DO NOT INSTALL (score {res['score']})"
    other_gaps = sorted({g["reason"] for g in gaps if g["reason"] not in BENIGN_GAPS})
    extra = f"; not fully analysed: {', '.join(other_gaps)}" if other_gaps else ""
    return "review", f"SkillSpector: {res['recommendation']} (score {res['score']}){extra}"


def format_result(res: dict, status: str, reason: str, max_issues: int = 6) -> str:
    sig = {"verified": "NVIDIA signature verified", "none": "", "not-checked": "signature not checked",
           "extra-files": "NVIDIA signature: extra unsigned files", "failed": "NVIDIA SIGNATURE FAILED"}
    head = (f"  {status.upper():<10} {res['name']} ({res['kind']})  score {res['score']} "
            f"{res['recommendation']}  {sig.get(res['signature'], res['signature'])}").rstrip()
    lines = [head, f"             {reason}"]
    if status not in OK_STATUSES:
        shown = sorted(res["issues"], key=lambda i: i["severity"] not in HIGH)[:max_issues]
        for i in shown:
            lines.append(f"             - {i['severity']} {i['id']} {i['pattern']} @ {i['where']}: {i['finding']}")
        if len(res["issues"]) > len(shown):
            lines.append(f"             - ... {len(res['issues']) - len(shown)} more")
        lines.append(f"             files: {res['path']}")
    return "\n".join(lines)


# -------------------------------------------------------------------- scan

GITHUB_SHORT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def materialize(source: str) -> tuple[Path, str, list[str]]:
    """Return (local dir, canonical name, identifiers the gate can match)."""
    p = Path(source).expanduser()
    if p.exists():
        p = p.resolve()
        return p, str(p), [str(p)]
    url = source
    if GITHUB_SHORT.match(source):
        url = f"https://github.com/{source}"
    if not re.match(r"^(https://|git@|ssh://)", url):
        raise GuardError(f"{source} is not a path, a git URL or a GitHub owner/repo")
    m = re.search(r"github\.com[/:]([^/]+)/([^/#?]+?)(?:\.git)?/?$", url)
    ids = [url, source]
    canonical = url
    if m:
        short = f"{m.group(1)}/{m.group(2)}".lower()
        canonical = f"github.com/{short}"
        ids += [short, canonical]
    dest = STATE_DIR / "src" / hashlib.sha256(url.encode()).hexdigest()[:16]
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
                       capture_output=True, text=True, timeout=900, env=env, check=False)
    if r.returncode != 0:
        raise GuardError(f"could not clone {url}: {tail(r.stderr)}")
    return dest, canonical, sorted(set(ids))


def cmd_scan(args) -> int:
    local, canonical, ids = materialize(args.source)
    units = find_units(local)
    if args.skill:
        wanted = {s.lower() for s in args.skill}
        units = [u for u in units if u["name"].lower() in wanted or u["dir"].lower() in wanted]
        missing = wanted - {u["name"].lower() for u in units} - {u["dir"].lower() for u in units}
        if missing:
            raise GuardError(f"not found in {canonical}: {', '.join(sorted(missing))}")
    if not units:
        raise GuardError(f"no SKILL.md or .claude-plugin/plugin.json found in {canonical}")
    if len(units) > MAX_UNITS_WITHOUT_SKILL:
        raise GuardError(f"{canonical} holds {len(units)} skills/plugins: pick them with --skill NAME")

    entries = approved_entries()
    results = []
    print(f"skill-guard scan: {canonical} ({len(units)} to check, files in {local})")
    for u in units:
        res = evaluate(u)
        status, reason = decide(res, entries)
        res.update(status=status, reason=reason)
        results.append(res)
        print(format_result(res, status, reason))

    scans = load_json(SCANS_FILE, [])
    scans = [s for s in scans if s.get("source") != canonical or set(s.get("labels", [])) != set(args.label)]
    scans.append({
        "source": canonical,
        "identifiers": ids + list(args.label),
        "labels": list(args.label),
        "scanned_at": now().isoformat(timespec="seconds"),
        "units": [{k: r[k] for k in ("name", "dir", "kind", "status", "score", "recommendation",
                                     "signature", "sha256", "path")} for r in results],
    })
    save_json(SCANS_FILE, scans)

    bad = [r for r in results if r["status"] not in OK_STATUSES]
    if not bad:
        print("Verdict: PASS. The install may go ahead.")
        return 0
    blocked = any(r["status"] == "blocked" for r in bad)
    print("Verdict: " + ("BLOCKED" if blocked else "REVIEW") + ". Do not install until every finding "
          f"above is checked in the files and the user explicitly approves ({POLICY}).")
    return 1


# ------------------------------------------------------------------- check

def installed_roots() -> list[tuple[Path, tuple[str, ...]]]:
    home = Path.home()
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    roots = [
        (home / ".claude" / "skills", ()),
        # marketplaces/ holds catalogs, not installed plugins.
        (home / ".claude" / "plugins", ("marketplaces",)),
        (project / ".claude" / "skills", ()),
        (home / ".agents" / "skills", ()),
    ]
    seen, out = set(), []
    for root, exclude in roots:
        key = root.resolve() if root.exists() else root
        if key not in seen:
            seen.add(key)
            out.append((root, exclude))
    return out


def check_installed() -> list[dict]:
    entries = approved_entries()
    results = []
    for root, exclude in installed_roots():
        for u in find_units(root, exclude):
            sha = dir_hash(Path(u["path"]))
            same = [e for e in entries if e.get("name") == u["name"] and e.get("kind") == u["kind"]]
            if any(e.get("sha256") == sha for e in same):
                results.append({**u, "sha256": sha, "status": "approved", "reason": "approved"})
                continue
            try:
                res = evaluate(u)
            except GuardError as exc:
                results.append({**u, "sha256": sha, "status": "error", "reason": str(exc)})
                continue
            status, reason = decide(res, entries)
            results.append({**res, "status": status, "reason": reason})
    return results


def cmd_check(_args) -> int:
    results = check_installed()
    print(f"skill-guard check: {len(results)} installed skills/plugins")
    for r in results:
        if "score" in r:
            print(format_result(r, r["status"], r["reason"]))
        else:
            print(f"  {r['status'].upper():<10} {r['name']} ({r['kind']})  {r['reason']}")
    return 0 if all(r["status"] in OK_STATUSES for r in results) else 1


# ----------------------------------------------------------------- approve

def cmd_approve(args) -> int:
    path = Path(args.path).expanduser().resolve()
    units = find_units(path)
    if not units:
        raise GuardError(f"no skill or plugin at {path}")
    if len(units) > 1 and not args.all:
        names = ", ".join(u["name"] for u in units)
        raise GuardError(f"{path} holds {len(units)} skills/plugins ({names}); pass --all to approve each")
    data = load_json(APPROVED_FILE, {"entries": []})
    entries = data.setdefault("entries", [])
    for u in units:
        res = evaluate(u)
        if res["signature"] == "failed":
            raise GuardError(f"{u['name']}: NVIDIA signature does not verify; it cannot be approved")
        entries[:] = [e for e in entries if not (e.get("name") == res["name"] and e.get("kind") == res["kind"])]
        entries.append({
            "name": res["name"],
            "kind": res["kind"],
            "sha256": res["sha256"],
            "score": res["score"],
            "recommendation": res["recommendation"],
            "signature": res["signature"],
            "accepted_findings": sorted({i["fingerprint"] for i in res["issues"]}),
            "approved_on": today(),
            "note": args.note,
        })
        print(f"approved: {res['name']} ({res['kind']}) score {res['score']} {res['recommendation']}")
    entries.sort(key=lambda e: (e["kind"], e["name"]))
    data["policy"] = "README.md"
    save_json(APPROVED_FILE, data)
    return 0


# ----------------------------------------------------------- session start

def cmd_session_start(_args) -> int:
    problems = setup() if os.environ.get("CLAUDE_CODE_REMOTE") == "true" else []
    if not tool("skillspector"):
        print("skill-guard: SkillSpector is not installed here, so installed skills were not scanned. "
              f"Run `{CLI} setup`, then `{CLI} check`.")
        return 0
    try:
        results = check_installed()
    except Exception as exc:  # noqa: BLE001 - never block the session
        print(f"skill-guard: the installed-skills check failed: {exc}")
        return 0
    ok = [r for r in results if r["status"] in OK_STATUSES]
    bad = [r for r in results if r["status"] not in OK_STATUSES]
    lines = [f"skill-guard: {len(results)} installed skills/plugins checked, {len(ok)} approved or passed."]
    for p in problems:
        lines.append(f"skill-guard setup problem: {p}")
    if bad:
        lines.append(
            "skill-guard WARNING: these installed skills/plugins have not passed the mandatory scan "
            f"({POLICY}). Do not use them until they are reviewed and the user approves them. "
            "Tell the user about this at the start of your next reply."
        )
        for r in bad:
            lines.append(format_result(r, r["status"], r["reason"], max_issues=4) if "score" in r
                         else f"  {r['status'].upper()} {r['name']} ({r['kind']}): {r['reason']}")
    print("\n".join(lines))
    return 0


# -------------------------------------------------------------------- gate

INSTALL_PATTERNS = [
    re.compile(r"\b(?:npx|bunx|pnpx)\s+(?:-y\s+|--yes\s+)*skills(?:@\S+)?\s+(?:add|install|i)\b"),
    re.compile(r"\bpnpm\s+dlx\s+skills(?:@\S+)?\s+(?:add|install|i)\b"),
    re.compile(r"\bclaude\s+plugins?\s+(?:install|i|marketplace\s+add)\b"),
]
SKILL_DIR = re.compile(r"\.claude/(?:skills|plugins)\b|\.agents/skills\b")
REDIRECT_INTO_SKILL_DIR = re.compile(r">>?\s*['\"]?\S*(?:\.claude/(?:skills|plugins)|\.agents/skills)")
SEGMENTS = re.compile(r"&&|\|\||[;|\n]")
COPY_TOOLS = {"cp", "mv", "rsync", "ln", "install", "git", "unzip", "tar", "curl", "wget", "tee"}
LISTING = re.compile(r"\s(?:--list|-l)\b|\bskills(?:@\S+)?\s+(?:find|search|list|ls)\b")


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _flag_values(args: list[str], flags: tuple[str, ...]) -> list[str]:
    values = [args[i + 1] for i, a in enumerate(args[:-1]) if a in flags]
    values += [a.split("=", 1)[1] for a in args if "=" in a and a.split("=", 1)[0] in flags]
    return values


def writes_into_skill_dir(command: str) -> bool:
    """Heuristic: does a copy, clone, download or unpack land in a skills folder?"""
    inside = False  # after `cd` into a skills folder
    for seg in SEGMENTS.split(command):
        if REDIRECT_INTO_SKILL_DIR.search(seg):
            return True
        toks = _tokens(seg)
        while toks and (toks[0] in ("sudo", "env", "command") or re.match(r"^\w+=", toks[0])):
            toks = toks[1:]
        if not toks:
            continue
        cmd, args = os.path.basename(toks[0]), toks[1:]
        if cmd == "cd":
            inside = bool(args and SKILL_DIR.search(args[-1]))
            continue
        if cmd not in COPY_TOOLS or (cmd == "git" and args[:1] != ["clone"]):
            continue
        if cmd in ("cp", "mv", "rsync", "ln", "install"):
            targets = args[-1:]
        elif cmd == "git":
            targets = args[-1:] if len(args) > 2 else []
        elif cmd == "unzip":
            targets = _flag_values(args, ("-d",))
        elif cmd == "tar":
            targets = _flag_values(args, ("-C", "--directory"))
        elif cmd in ("curl", "wget"):
            targets = _flag_values(args, ("-o", "--output", "-O", "--output-document", "-P", "--directory-prefix"))
        else:  # tee
            targets = [a for a in args if not a.startswith("-")]
        if inside or any(SKILL_DIR.search(t) for t in targets):
            return True
    return False


def installer_command(command: str) -> bool:
    return any(p.search(command) for p in INSTALL_PATTERNS)


def install_intent(command: str) -> bool:
    if "skill_guard.py" in command or re.search(r"(?:^|[\s;&|(])skill-guard\s", command):
        return False
    if installer_command(command):
        return not LISTING.search(command)
    return writes_into_skill_dir(command)


def requested_names(command: str) -> list[str]:
    if not installer_command(command):
        return []
    names = []
    for m in re.finditer(r"(?:--skill|-s)(?:=|\s+)([^\s;&|]+)", command):
        names += [n for n in m.group(1).strip("'\"").split(",") if n]
    m = re.search(r"\bclaude\s+plugins?\s+(?:install|i)\s+([^\s;&|@]+)", command)
    if m:
        names.append(m.group(1).strip("'\""))
    return [n.lower() for n in names]


def mentions(command: str, ident: str) -> bool:
    ident = ident.lower().rstrip("/")
    if len(ident) < 3:
        return False
    return re.search(r"(?<![\w./-])" + re.escape(ident) + r"(?![\w-])", command.lower()) is not None


def gate_decision(command: str) -> tuple[str, str] | None:
    """None = allow. Otherwise (decision, reason) with decision deny or ask."""
    if not install_intent(command):
        return None
    names = requested_names(command)
    scans = load_json(SCANS_FILE, [])
    matches = []
    for s in scans:
        idents = list(s.get("identifiers") or []) + [u.get("path", "") for u in s.get("units", [])]
        named_plugin = names and any(
            u["name"].lower() in names and u["kind"] == "plugin" for u in s.get("units", [])
        )
        if named_plugin or any(mentions(command, i) for i in idents if i):
            matches.append(s)
    how = (f"Scan it first: {CLI} scan <path | git URL | owner/repo> "
           f"[--skill NAME] [--label <plugin@marketplace>], then follow {POLICY}.")
    if not matches:
        return "deny", "skill-guard: this installs a skill or plugin that was not scanned in this session. " + how
    units = [u for s in matches for u in s.get("units", [])]
    if names:
        units = [u for u in units if u["name"].lower() in names or u.get("dir", "").lower() in names]
        if not units:
            return "deny", f"skill-guard: {', '.join(names)} was not part of any scan. " + how
    bad = [u for u in units if u["status"] not in OK_STATUSES]
    if not bad:
        return None
    detail = "; ".join(f"{u['name']}: {u['status']} ({u['recommendation']}, score {u['score']})" for u in bad)
    return "ask", ("skill-guard: the scan did not pass: " + detail +
                   ". Install only if every finding was checked in the files and you approve.")


def cmd_gate(_args) -> int:
    try:
        payload = json.load(sys.stdin)
        command = str((payload.get("tool_input") or {}).get("command") or "")
    except (ValueError, AttributeError):
        return 0
    if payload.get("tool_name") != "Bash" or not command:
        return 0
    try:
        decision = gate_decision(command)
    except Exception as exc:  # noqa: BLE001 - fail closed on installs
        decision = ("deny", f"skill-guard could not check this install ({exc}).")
    if decision:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision[0],
            "permissionDecisionReason": decision[1],
        }}))
    return 0


# ----------------------------------------------------------- install hooks

def cmd_install_hooks(_args) -> int:
    """Add (or refresh) the skill-guard hooks in the user's Claude Code settings."""
    settings = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    data: dict = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise GuardError(f"{settings} is not valid JSON, so it was left alone: {exc}") from exc
    hooks = data.setdefault("hooks", {})
    me = f'python3 -I "{HERE / "skill_guard.py"}"'
    wanted = [("SessionStart", None, "session-start", 900), ("PreToolUse", "Bash", "gate", 30)]
    for event, matcher, sub, timeout in wanted:
        groups = hooks.setdefault(event, [])
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if "skill_guard.py" not in str(h.get("command", ""))]
        groups[:] = [g for g in groups if g.get("hooks")]
        entry: dict = {"hooks": [{"type": "command", "command": f"{me} {sub}", "timeout": timeout}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        groups.append(entry)
    save_json(settings, data)
    print(f"hooks installed in {settings}")
    return 0


# --------------------------------------------------------- install plugins

def cmd_install_plugins(_args) -> int:
    """Install the plugins in plugins.json. Each one passed a scan before it was listed;
    the session-start check scans the installed copy again."""
    entries = load_json(PLUGINS_FILE, {}).get("plugins", [])
    if not entries:
        print("no plugins listed")
        return 0
    claude = tool("claude") or ("/opt/claude-code/bin/claude" if Path("/opt/claude-code/bin/claude").exists() else None)
    if not claude:
        print("skill-guard: the claude command is missing, so listed plugins were not installed")
        return 0
    failed = 0
    for e in entries:
        plugin, marketplace = e.get("plugin", ""), e.get("marketplace", "")
        if not plugin or not marketplace:
            continue
        steps = [[claude, "plugin", "marketplace", "add", marketplace], [claude, "plugin", "install", plugin]]
        for step in steps:
            r = subprocess.run(step, capture_output=True, text=True, timeout=300, check=False)
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0 and "already" not in out.lower():
                print(f"skill-guard: {' '.join(step[1:])} failed: {tail(out, 2)}")
                failed += 1
                break
        else:
            print(f"plugin ready: {plugin}")
    return 1 if failed else 0


# -------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skill_guard.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sp = sub.add_parser("scan")
    sp.add_argument("source")
    sp.add_argument("--skill", action="append", default=[])
    sp.add_argument("--label", action="append", default=[],
                    help="another name the install command uses, e.g. plugin@marketplace")
    sub.add_parser("check")
    ap_ = sub.add_parser("approve")
    ap_.add_argument("path")
    ap_.add_argument("--note", required=True, help="who approved it and why the findings are acceptable")
    ap_.add_argument("--all", action="store_true", help="approve every skill/plugin under PATH")
    sub.add_parser("session-start")
    sub.add_parser("gate")
    sub.add_parser("install-hooks")
    sub.add_parser("install-plugins")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "setup":
            problems = setup()
            for p in problems:
                print(p)
            print("ready" if not problems else "setup incomplete")
            return 1 if problems else 0
        return {"scan": cmd_scan, "check": cmd_check, "approve": cmd_approve,
                "session-start": cmd_session_start, "gate": cmd_gate,
                "install-hooks": cmd_install_hooks, "install-plugins": cmd_install_plugins}[args.cmd](args)
    except GuardError as exc:
        print(f"skill-guard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
