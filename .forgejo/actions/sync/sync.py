"""sync: promote a tagged branch from this Forgejo fork to its GitHub upstream as a PR.

Triggered by pushing a tag sync/<name> (only allowed users can; protect sync/* in Forgejo).
sync/dry-run/<name> runs every step except the push and the PR. Steps:
  1. fetch GitHub main; rebase the tagged commits onto it (conflict -> stop, list files)
  2. guards: .github/workflows changes (push those from a workstation); "Closes/Fixes #N" must be open
     GitHub issues; mentions of a *draft* security advisory (undisclosed fix -> use its private fork);
     open secret-scanning alerts; dependency vulnerabilities introduced (pip-audit on uv.lock, main vs
     branch); open Dependabot alerts the branch resolves
  3. the repo's tests (SYNC_TEST_CMD)
  4. push the result to GitHub branch sync/<name> (never main) and open or update the PR to main
Env: GH_REPO (owner/name), GH_TOKEN (fine-grained, this repo: Contents/Pull requests RW; Issues, statuses,
checks, Dependabot alerts, secret-scanning alerts, security advisories R; no Workflows), TAG, SYNC_TEST_CMD,
RUN_URL. Exit 1 = promotion refused (reasons printed and in the job summary).
"""
import json, os, re, subprocess, sys, tomllib, urllib.error, urllib.parse, urllib.request

E = os.environ
REPO, TOKEN, TAG = E["GH_REPO"], E.get("GH_TOKEN", ""), E["TAG"]
if not TOKEN:
    raise SystemExit("the GH_PUSH_TOKEN secret isn't visible to this repo (Forgejo secrets are per repo/org): "
                     "add it under Settings -> Actions -> Secrets")
API = "https://api.github.com"
problems, notes = [], []


def sh(*cmd, check=True, env=None, input=None):
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, input=input)
    if check and r.returncode:
        raise SystemExit(f"command failed: {' '.join(cmd[:3])}...: {r.stderr.strip()[-800:]}")
    return r.stdout


def gh(path, method="GET", body=None, ok404=False):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "forgejo-sync"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(); return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        if e.code == 403:  # missing permission or feature disabled: say which, don't crash
            notes.append(f"GitHub {path.split('?')[0]}: HTTP 403 (token permission or feature not enabled)"); return None
        raise SystemExit(f"GitHub {method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")


def git_auth():  # token only in a per-command header, never in a URL or the config file
    return ["-c", "http.https://github.com/.extraheader=AUTHORIZATION: basic " +
            __import__("base64").b64encode(f"x-access-token:{TOKEN}".encode()).decode()]


def locked_versions(lock_text):
    try:
        return {p["name"].lower(): p["version"] for p in tomllib.loads(lock_text).get("package", [])
                if "version" in p and not {"editable", "virtual"} & set((p.get("source") or {}).keys())}  # skip the project itself
    except Exception:
        return {}


def vkey(v):  # good enough for "is version >= patched" on PyPI versions
    return [int(x) if x.isdigit() else x for x in re.split(r"[.+-]", v)]


def audit(lock_text):
    """Vulnerability IDs (pip-audit, OSV/PyPI advisories) for the packages pinned in a uv.lock."""
    vers = locked_versions(lock_text)
    if not vers:
        return {}
    reqs = "\n".join(f"{n}=={v}" for n, v in vers.items())
    out = subprocess.run(["uvx", "--quiet", "pip-audit", "--no-deps", "--disable-pip", "-r", "/dev/stdin", "-f", "json",
                          "--progress-spinner", "off"], input=reqs, capture_output=True, text=True)
    try:
        deps = json.loads(out.stdout).get("dependencies", [])
    except Exception:
        notes.append("pip-audit gave no result: " + (out.stderr.strip()[-200:] or "?")); return {}
    return {v["id"]: f"{d['name']} {d['version']}" for d in deps for v in d.get("vulns", [])}


name = TAG[len("sync/"):]
dry = name.startswith("dry-run/")
if dry:
    name = name[len("dry-run/"):]
if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or name in ("main", "master") or ".." in name:
    raise SystemExit(f"bad sync name: {name!r}")
branch = f"sync/{name}"
print(f"sync: {TAG} -> GitHub {REPO} branch {branch}{' (dry run)' if dry else ''}")

# 1. GitHub main, rebase
sh("git", *git_auth(), "fetch", "--no-tags", "-q", f"https://github.com/{REPO}.git", "main:refs/remotes/github/main")
base = sh("git", "merge-base", "HEAD", "github/main").strip()
commits = sh("git", "rev-list", "--reverse", f"{base}..HEAD").split()
if not commits:
    raise SystemExit("nothing to sync: the tagged commit is already on GitHub main")
sh("git", "config", "user.name", "forgejo-sync"); sh("git", "config", "user.email", E.get("SYNC_COMMITTER_EMAIL") or "forgejo-sync@users.noreply.github.com")
r = subprocess.run(["git", "rebase", "-q", "github/main"], capture_output=True, text=True)
if r.returncode:
    conflicts = sh("git", "diff", "--name-only", "--diff-filter=U", check=False).split()
    subprocess.run(["git", "rebase", "--abort"])
    raise SystemExit("rebase onto GitHub main failed (resolve on the branch and re-tag): " +
                     (", ".join(conflicts) if conflicts else r.stderr.strip()[-400:]))
changed = sh("git", "diff", "--name-only", "github/main..HEAD").split()
log = sh("git", "log", "--format=%H%n%B%n---", "github/main..HEAD")
print(f"{len(commits)} commit(s), {len(changed)} file(s) changed after rebase")

# 2. guards
# Outbound privacy: nothing may reveal the fork's private host, the tailnet, private addresses or a personal
# e-mail upstream. Checked on everything that leaves: commit identities, messages, the diff's added lines,
# and (below) the PR title/body. Added after a PR body linked the Forgejo run and commits carried a personal
# address (2026-10-31).
private = [re.escape(urllib.parse.urlparse(E.get("FORGEJO_URL", "")).hostname or "") or None,
           r"[a-z0-9-]+\.ts\.net\b", r"\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d+\.\d+\b",
           r"\b192\.168\.\d+\.\d+\b", r"\b10\.\d+\.\d+\.\d+\b", r"\bfd7a:115c:" r"a1e0:"]   # split so this source line doesn't match itself
private += [p for p in (E.get("SYNC_PRIVATE_PATTERNS") or "").split(",") if p.strip()]
private = [re.compile(p, re.I) for p in private if p]
def leaks(text):
    return sorted({m.group(0) for rx in private for m in rx.finditer(text)})
allowed_mail = [m.strip().lower() for m in (E.get("SYNC_ALLOWED_EMAILS") or "").split(",") if m.strip()]
for line in sh("git", "log", "--format=%h %ae %ce", "github/main..HEAD").splitlines():
    h, ae, ce = line.split()
    for who, mail in (("author", ae), ("committer", ce)):
        if not (mail.lower().endswith("@users.noreply.github.com") or mail.lower() in allowed_mail):
            problems.append(f"commit {h} {who} e-mail {mail} isn't a GitHub noreply address (or in SYNC_ALLOWED_EMAILS)")
added = "\n".join(l[1:] for l in sh("git", "diff", "github/main..HEAD").splitlines() if l.startswith("+") and not l.startswith("+++"))
for where, text in (("commit messages", log), ("added lines", added), ("branch name", branch)):
    if leaks(text):
        problems.append(f"private names/addresses in {where}: " + ", ".join(leaks(text)))
wf = [f for f in changed if f.startswith(".github/workflows/")]
if wf:
    problems.append("touches .github/workflows (cherry-pick to a workstation and push it yourself): " + ", ".join(wf))

refs = sorted({int(n) for n in re.findall(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", log)})
closes = []
for n in refs:
    i = gh(f"/repos/{REPO}/issues/{n}", ok404=True)
    if not i or "pull_request" in i:
        problems.append(f"#{n} is referenced as closed but isn't a GitHub issue")
    elif i.get("state") != "open":
        problems.append(f"#{n} is already {i.get('state')}")
    else:
        closes.append(f"Closes #{n} ({i['title']})")

text = (log + "\n" + sh("git", "diff", "github/main..HEAD")).lower()
for adv in gh(f"/repos/{REPO}/security-advisories?state=draft&per_page=100") or []:
    if adv["ghsa_id"].lower() in text or (adv.get("summary") and adv["summary"].lower() in text):
        problems.append(f"mentions draft advisory {adv['ghsa_id']}: undisclosed fixes go through its private fork, not a public PR")
fixes_ghsa = [f"Fixes {a['ghsa_id']} ({a.get('summary','')})" for a in gh(f"/repos/{REPO}/security-advisories?state=published&per_page=100") or []
              if a["ghsa_id"].lower() in text]

secrets = gh(f"/repos/{REPO}/secret-scanning/alerts?state=open&per_page=100", ok404=True)
if secrets is None:
    notes.append("secret scanning isn't enabled on the GitHub repo (no alerts to check)")
elif secrets:
    problems.append(f"{len(secrets)} open secret-scanning alert(s) on GitHub: resolve them first")

lock_main = sh("git", "show", "github/main:uv.lock", check=False)
lock_branch = open("uv.lock").read() if os.path.exists("uv.lock") else ""
vuln_lines = []
if lock_branch:
    before, after = audit(lock_main), audit(lock_branch)
    introduced = {k: v for k, v in after.items() if k not in before}
    fixed = {k: v for k, v in before.items() if k not in after}
    if introduced:
        problems.append("introduces vulnerable dependencies: " + ", ".join(f"{k} ({v})" for k, v in introduced.items()))
    vuln_lines += [f"Fixes {k} ({v})" for k, v in fixed.items()]
    vers = locked_versions(lock_branch)
    dependabot = gh(f"/repos/{REPO}/dependabot/alerts?state=open&per_page=100", ok404=True)
    if dependabot is None:
        notes.append("Dependabot alerts aren't enabled on the GitHub repo (none to match)")
    for a in dependabot or []:
        pkg = a["dependency"]["package"]["name"].lower(); patched = (a.get("security_vulnerability") or {}).get("first_patched_version") or {}
        have = vers.get(pkg)
        if have and patched.get("identifier") and vkey(have) >= vkey(patched["identifier"]):
            vuln_lines.append(f"Resolves Dependabot alert #{a['number']} ({pkg} {have} >= {patched['identifier']})")
        local = {v.split()[0].lower() for v in after.values()}
        if pkg not in local and not (have and patched.get("identifier") and vkey(have) >= vkey(patched["identifier"])):
            notes.append(f"Dependabot alert #{a['number']} ({pkg}) not seen by pip-audit")

# 3. tests
cmd = E.get("SYNC_TEST_CMD") or "uv sync --locked && uv run python -m pytest -q -p no:cacheprovider"
print(f"tests: {cmd}")
t = subprocess.run(["bash", "-c", cmd])
if t.returncode:
    problems.append(f"tests failed ({cmd})")

# summary
# No Forgejo URL in the PR: it would publish the fork's private host name upstream (log only).
if E.get("RUN_URL"):
    print(f"run: {E['RUN_URL']}")
summary = [f"Promoted from the maintainer's fork by `{TAG}` ({len(commits)} commit(s), rebased onto main, tested in a throwaway VM).",
           ""] + closes + fixes_ghsa + vuln_lines
if notes:
    summary += ["", "Notes:"] + [f"- {n}" for n in notes]
title = sh("git", "log", "-1", "--format=%s", "HEAD~%d" % (len(commits) - 1)).strip()   # first commit's subject
body = "\n".join(summary) + "\n"
for where, text in (("PR title", title), ("PR body", body)):
    if leaks(text):
        problems.append(f"private names/addresses in the {where}: " + ", ".join(leaks(text)))
print(f"--- PR title (exact) ---\n{title}\n--- PR body (exact) ---\n{body}--- end ---")
if problems:
    print("\nPROMOTION REFUSED:\n" + "\n".join(f"- {p}" for p in problems))
    with open(E.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
        f.write("## Sync refused\n" + "\n".join(f"- {p}" for p in problems) + "\n")
    sys.exit(1)
if dry:
    print(f"\ndry run: would push {branch} and open/update its PR"); sys.exit(0)

# 4. push + PR
# Lease against what the GitHub branch holds right now (empty = must not exist yet): a URL push has no
# remote-tracking ref for a plain --force-with-lease to compare with.
current = sh("git", *git_auth(), "ls-remote", f"https://github.com/{REPO}.git", f"refs/heads/{branch}").split()
sh("git", *git_auth(), "push", "-q", f"--force-with-lease=refs/heads/{branch}:{current[0] if current else ''}",
   f"https://github.com/{REPO}.git", f"HEAD:refs/heads/{branch}")
owner = REPO.split("/")[0]
existing = gh(f"/repos/{REPO}/pulls?head={owner}:{branch}&state=open") or []
if existing:
    pr = gh(f"/repos/{REPO}/pulls/{existing[0]['number']}", "PATCH", {"title": title, "body": body})
else:
    pr = gh(f"/repos/{REPO}/pulls", "POST", {"title": title, "head": branch, "base": "main", "body": body})
print(f"\nPR: {pr.get('html_url')}")
