"""sync: promote a tagged branch from this Forgejo fork to its GitHub upstream as a PR.

Triggered by pushing a tag sync/<name> (only allowed users can; protect sync/* in Forgejo).
sync/dry-run/<name> runs every step except the push and the PR. The same runs from the Actions tab
(Run workflow, pick the branch; inputs: name, confirm: type push to push, blank = dry run). Steps:
  0. stop if the commit under test isn't the branch's head now (a re-run of an old run, a tag left behind)
  1. fetch GitHub main; rebase the tagged commits onto it, only if the branch isn't already on it
     (conflict -> stop, list files)
  2. guards: .github/workflows changes (push those from a workstation); "Closes/Fixes #N" must be open
     GitHub issues; mentions of a *draft* security advisory (undisclosed fix -> use its private fork);
     open secret-scanning alerts; dependency vulnerabilities introduced (pip-audit on uv.lock, main vs
     branch); open Dependabot alerts the branch resolves
  3. the repo's tests (SYNC_TEST_CMD), with an allowlisted environment: no GH_TOKEN
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


def redact(text):  # the token, raw or base64 (as in git_auth's header), never reaches the log
    for secret in filter(None, (TOKEN, __import__("base64").b64encode(f"x-access-token:{TOKEN}".encode()).decode()
                                if TOKEN else "")):
        text = text.replace(secret, "***")
    return text


def sh(*cmd, check=True, env=None, input=None):
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, input=input)
    if check and r.returncode:
        shown = [a for a in cmd if not a.startswith("http.") and a != "-c"][:3]   # skip git_auth's -c header
        raise SystemExit(redact(f"command failed: {' '.join(shown)}...: {r.stderr.strip()[-800:]}"))
    return r.stdout


class GitHubError(SystemExit):  # uncaught, it stops the run like any other SystemExit
    pass


def gh(path, method="GET", body=None, ok404=False, ok403=False, links=None):
    """ok404/ok403: that status means "nothing there" (None). A 403 is only tolerable where the answer is
    informational: a guard that can't see (token without the permission) must refuse, not pass (review F3).
    links: a dict to receive the response's Link header relations (pagination)."""
    url = path if path.startswith(API + "/") else API + path
    where = f"GitHub {method} {url[len(API):].split('?')[0]}"
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "forgejo-sync"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if links is not None:
                links.update({rel: u for u, rel in re.findall(r'<([^>]+)>;\s*rel="(\w+)"', r.headers.get("Link") or "")})
            data = r.read(); return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        if ok403 and e.code == 403:  # missing permission or feature disabled: say which, don't crash
            notes.append(f"{where}: HTTP 403 (token permission or feature not enabled)"); return None
        detail = e.read().decode(errors="replace")[:300] if e.fp else e.reason
        raise GitHubError(f"{where}: HTTP {e.code} {detail}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GitHubError(f"{where}: {e}")


def gh_all(path, ok404=False, ok403=False, max_pages=50):
    """Every item of a list endpoint, 100 per page, following the Link header (page- and cursor-paginated
    endpoints alike). None if the first page is a tolerated 404/403."""
    items, url = [], path + ("&" if "?" in path else "?") + "per_page=100"
    for _ in range(max_pages):
        links = {}
        batch = gh(url, ok404=ok404, ok403=ok403, links=links)
        if batch is None:
            return items or None
        if not isinstance(batch, list):
            raise GitHubError(f"GitHub GET {path.split('?')[0]}: expected a list, got {type(batch).__name__}")
        items += batch
        url = links.get("next")
        if not url:
            return items
        if not url.startswith(API + "/"):   # the token goes only to the API
            raise GitHubError(f"GitHub GET {path.split('?')[0]}: next page outside {API}: {url}")
    raise GitHubError(f"GitHub GET {path.split('?')[0]}: more than {max_pages} pages")


def guard_list(path, what, ok404=False):
    """A list a guard depends on. Any failure to read it (403, network, too many pages) refuses promotion."""
    try:
        return gh_all(path, ok404=ok404)
    except GitHubError as e:
        problems.append(f"can't check {what}, so promotion is refused: {e} "
                        "(check GH_PUSH_TOKEN's permissions, see sync.yml)")
        return []


def git_auth():  # token only in a per-command header, never in a URL or the config file
    return ["-c", "http.https://github.com/.extraheader=AUTHORIZATION: basic " +
            __import__("base64").b64encode(f"x-access-token:{TOKEN}".encode()).decode()]


# What the branch's tests may see. They run the promoted branch's code, conftest and dependencies, so they get an
# allowlist: never GH_TOKEN or the runner's tokens. Names that look secret are dropped even if listed.
TEST_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR", "CI",
            "PYTHONUNBUFFERED", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
            "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
TEST_ENV_PREFIXES = ("LC_", "UV_", "XDG_")


def test_env(environ):
    secret = re.compile(r"TOKEN|SECRET|PASSW|KEY|CREDENTIAL|AUTH", re.I)
    return {k: v for k, v in environ.items()
            if (k in TEST_ENV or k.startswith(TEST_ENV_PREFIXES)) and not secret.search(k)}


def locked_versions(lock_text):
    try:
        return {p["name"].lower(): p["version"] for p in tomllib.loads(lock_text).get("package", [])
                if "version" in p and not {"editable", "virtual"} & set((p.get("source") or {}).keys())}  # skip the project itself
    except Exception:
        return {}


def vkey(v):
    """PEP 440 sort key (epoch, release, pre, post, dev, local), like packaging.version.Version, which this
    script can't import (uv run --no-project: no dependencies). Raises ValueError on a non-PEP 440 version."""
    m = re.fullmatch(r"v?(?:(\d+)!)?(\d+(?:\.\d+)*)"
                     r"(?:[._-]?(a|alpha|b|beta|c|rc|pre|preview)[._-]?(\d*))?"
                     r"(?:-(\d+)|[._-]?(post|rev|r)[._-]?(\d*))?"
                     r"(?:[._-]?(dev)[._-]?(\d*))?"
                     r"(?:\+([a-z0-9]+(?:[._-][a-z0-9]+)*))?", v.strip().lower())
    if not m:
        raise ValueError(f"not a PEP 440 version: {v!r}")
    epoch, rel, pre_l, pre_n, post_implicit, post_l, post_n, dev_l, dev_n, local = m.groups()
    release = [int(x) for x in rel.split(".")]
    while len(release) > 1 and release[-1] == 0:   # 1.0 == 1.0.0
        release.pop()
    if pre_l:
        pre = (0, {"a": 0, "alpha": 0, "b": 1, "beta": 1}.get(pre_l, 2), int(pre_n or 0))
    elif dev_l and not (post_l or post_implicit):
        pre = (-1, 0, 0)   # 1.0.dev1 < 1.0a1
    else:
        pre = (1, 0, 0)    # a final release sorts after its pre-releases
    post = (0, int(post_implicit or post_n or 0)) if (post_l or post_implicit) else (-1, 0)
    dev = (0, int(dev_n or 0)) if dev_l else (1, 0)
    local_key = tuple((1, int(p), "") if p.isdigit() else (0, 0, p) for p in re.split(r"[._-]", local)) if local else ()
    return (int(epoch or 0), tuple(release), pre, post, dev, local_key)


def at_least(have, patched):
    """have >= patched; False when either isn't a PEP 440 version (then the alert isn't claimed as resolved)."""
    try:
        return vkey(have) >= vkey(patched)
    except ValueError:
        return False


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


if E.get("SYNC_EVENT") == "workflow_dispatch":
    # "Run workflow" button: the branch picked in the dialog's "Use workflow from". This script itself comes from
    # main (or the checks_from input), not from that branch: see sync.yml.
    src = E.get("SYNC_BRANCH", "")
    if src in ("", "main", "master"):
        sys.exit("sync: pick the branch to promote in the Run workflow dialog's \"Use workflow from\" "
                 f"(got '{src or '?'}')")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", src) or ".." in src or src.startswith(("-", "/")):
        sys.exit(f"sync: bad branch name {src!r}")
    name = (E.get("SYNC_NAME") or "").strip() or src.replace("/", "-")
    # One control (owner, 2026-11-06): typing push in confirm pushes, blank is a dry run, anything else stops.
    # Replaces mode + confirm (added after inputs went missing, 2026-11-01/02). Fail-safe direction: inputs that
    # don't arrive mean a dry run, never a push; the log says DRY RUN. Forgejo drops blank fields, so a dry run
    # may record no inputs at all.
    raw = (E.get("SYNC_INPUTS") or "").strip()
    confirm = (E.get("SYNC_CONFIRM") or "").strip().lower()
    if confirm not in ("", "push"):
        sys.exit(f"sync: confirm must be push (push for real) or blank (dry run), got {confirm!r}. Nothing was pushed. "
                 f"The dialog sent: {raw or 'nothing'}")
    dry = confirm != "push"
    print(f"sync: dialog inputs {raw or 'none'} -> {'DRY RUN (confirm blank): nothing will be pushed' if dry else 'push'}")
    TAG = f"sync/{'dry-run/' if dry else ''}{name}"   # same label as the tag route, for messages and the PR
else:
    name = TAG[len("sync/"):]
    dry = name.startswith("dry-run/")
    if dry:
        name = name[len("dry-run/"):]
if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or name in ("main", "master") or ".." in name:
    raise SystemExit(f"bad sync name: {name!r}")
branch = f"sync/{name}"
print(f"sync: {TAG} -> GitHub {REPO} branch {branch}{' (dry run)' if dry else ''}")

# 0. Test what the branch holds now. A re-run replays its original commit, and a tag stays where it was put, so
# both can test code the branch has since replaced (2026-11-02: two re-runs refused a fix the branch already had).
tested = sh("git", "rev-parse", "HEAD").strip()
heads = {ref[len("refs/heads/"):]: sha for sha, ref in
         (l.split("\t") for l in sh("git", "ls-remote", "--heads", "origin").splitlines() if "\t" in l)}
src_branch = src if E.get("SYNC_EVENT") == "workflow_dispatch" else \
    next((b for b in heads if b.replace("/", "-") == name or b == name), None)
if src_branch is None:
    print(f"sync: no branch in this repo matches {name!r}; testing {TAG} at {tested[:7]} as tagged")
elif heads.get(src_branch) != tested:
    sys.exit(f"sync: stale run: this tests {tested[:7]}, but branch {src_branch} is at "
             f"{(heads.get(src_branch) or 'nothing (deleted)')[:7]}. "
             + ("Start a new run (Run workflow) instead of re-running an old one."
                if E.get("SYNC_EVENT") == "workflow_dispatch" else
                f"Move the tag to the branch head (git fetch origin && git tag -f {TAG} origin/{src_branch} && "
                f"git push -f origin {TAG}), "
                "or use the Run workflow button."))
else:
    print(f"sync: {src_branch} is at {tested[:7]}, the commit under test")

# 1. GitHub main, rebase
sh("git", *git_auth(), "fetch", "--no-tags", "-q", f"https://github.com/{REPO}.git", "main:refs/remotes/github/main")
base = sh("git", "merge-base", "HEAD", "github/main").strip()
commits = sh("git", "rev-list", "--reverse", f"{base}..HEAD").split()
if not commits:
    raise SystemExit("nothing to sync: the tagged commit is already on GitHub main")
sh("git", "config", "user.name", "forgejo-sync"); sh("git", "config", "user.email", E.get("SYNC_COMMITTER_EMAIL") or "forgejo-sync@users.noreply.github.com")
# Rebase only when GitHub main has moved past the branch's base: a branch already on main goes up as is
# (same commits as Forgejo, nothing rewritten, nothing that can conflict).
main_sha = sh("git", "rev-parse", "github/main").strip()
rebased = base != main_sha
if rebased:
    print(f"rebase needed: branch is based on {base[:7]}, GitHub main is at {main_sha[:7]}")
    r = subprocess.run(["git", "rebase", "-q", "github/main"], capture_output=True, text=True)
    if r.returncode:
        conflicts = sh("git", "diff", "--name-only", "--diff-filter=U", check=False).split()
        subprocess.run(["git", "rebase", "--abort"])
        raise SystemExit(f"rebase of {base[:7]}..HEAD onto GitHub main {main_sha[:7]} failed "
                         "(rebase the branch on Forgejo and re-tag): " +
                         (", ".join(conflicts) if conflicts else r.stderr.strip()[-400:]))
else:
    print(f"no rebase needed: branch is already on GitHub main {main_sha[:7]}")
# Count and name what actually goes up, after any rebase: a rebase drops merge commits and commits already on
# main, so the count from before it can be too high (2026-11-11: #68 and #69 got #66's title from HEAD~N).
commits = sh("git", "rev-list", "--reverse", "--no-merges", "github/main..HEAD").split()
if not commits:
    raise SystemExit("nothing to sync: every commit on the branch is already on GitHub main")
# The PR title is the branch's own first commit: follow first parents so a merged-in branch's commits don't count.
own = sh("git", "rev-list", "--reverse", "--first-parent", "--no-merges", "github/main..HEAD").split() or commits
changed = sh("git", "diff", "--name-only", "github/main..HEAD").split()
log = sh("git", "log", "--format=%H%n%B%n---", "github/main..HEAD")
print(f"{len(commits)} commit(s), {len(changed)} file(s) changed{' after rebase' if rebased else ''}")

# 2. guards
# Outbound privacy: nothing may reveal the fork's private host, the tailnet, private addresses or a personal
# e-mail upstream. Checked on everything that leaves: commit identities, messages, the diff's added lines,
# and (below) the PR title/body. Added after a PR body linked the Forgejo run and commits carried a personal
# address (2026-10-31).
# IPv4: every part must be 0-255 and the address can't sit inside a longer dotted number, so version strings
# (a Windows build, a four-part package version) aren't taken for private addresses (2026-11-02: a test file's
# "not an address" cases were refused).
OCT = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
def ipv4(head, parts):  # head: the fixed leading part(s); parts: how many free octets follow
    return rf"(?<![\d.]){head}" + rf"\.{OCT}" * parts + r"(?!\.?\d)"
private = [re.escape(urllib.parse.urlparse(E.get("FORGEJO_URL", "")).hostname or "") or None,
           r"[a-z0-9-]+\.ts\.net\b", ipv4(r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])", 2),
           ipv4(r"192\.168", 2), ipv4(r"10", 3), r"\bfd7a:115c:" r"a1e0:"]   # split so this source line doesn't match itself
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
    try:
        i = gh(f"/repos/{REPO}/issues/{n}", ok404=True)
    except GitHubError as e:
        problems.append(f"can't check #{n}, so promotion is refused: {e}"); continue
    if not i or "pull_request" in i:
        problems.append(f"#{n} is referenced as closed but isn't a GitHub issue")
    elif i.get("state") != "open":
        problems.append(f"#{n} is already {i.get('state')}")
    else:
        closes.append(f"Closes #{n} ({i['title']})")

text = (log + "\n" + sh("git", "diff", "github/main..HEAD")).lower()
# Guards: a list that can't be read (403: token without the permission) refuses promotion; it never passes as
# empty (review 2026-11-09 F3). Published advisories and Dependabot alerts only add lines to the PR body.
for adv in guard_list(f"/repos/{REPO}/security-advisories?state=draft", "draft security advisories"):
    if adv["ghsa_id"].lower() in text or (adv.get("summary") and adv["summary"].lower() in text):
        problems.append(f"mentions draft advisory {adv['ghsa_id']}: undisclosed fixes go through its private fork, not a public PR")
fixes_ghsa = [f"Fixes {a['ghsa_id']} ({a.get('summary','')})"
              for a in gh_all(f"/repos/{REPO}/security-advisories?state=published", ok403=True) or []
              if a["ghsa_id"].lower() in text]

# 404 = secret scanning not enabled (nothing to check); 403 = can't see the alerts (refused)
secrets = guard_list(f"/repos/{REPO}/secret-scanning/alerts?state=open", "secret-scanning alerts", ok404=True)
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
    dependabot = gh_all(f"/repos/{REPO}/dependabot/alerts?state=open", ok404=True, ok403=True)
    if dependabot is None:
        notes.append("Dependabot alerts aren't enabled on the GitHub repo (none to match)")
    for a in dependabot or []:
        pkg = a["dependency"]["package"]["name"].lower(); patched = (a.get("security_vulnerability") or {}).get("first_patched_version") or {}
        have = vers.get(pkg)
        if have and patched.get("identifier") and at_least(have, patched["identifier"]):
            vuln_lines.append(f"Resolves Dependabot alert #{a['number']} ({pkg} {have} >= {patched['identifier']})")
        local = {v.split()[0].lower() for v in after.values()}
        if pkg not in local and not (have and patched.get("identifier") and at_least(have, patched["identifier"])):
            notes.append(f"Dependabot alert #{a['number']} ({pkg}) not seen by pip-audit")

# 3. tests
cmd = E.get("SYNC_TEST_CMD") or "uv sync --locked && uv run python -m pytest -q -p no:cacheprovider"
print(f"tests: {cmd}")
t = subprocess.run(["bash", "-c", cmd], env=test_env(E))   # without GH_TOKEN or other secrets: see test_env
if t.returncode:
    problems.append(f"tests failed ({cmd})")

# summary
# No Forgejo URL in the PR: it would publish the fork's private host name upstream (log only).
if E.get("RUN_URL"):
    print(f"run: {E['RUN_URL']}")
summary = [f"Promoted from the maintainer's fork by `{TAG}` ({len(commits)} commit(s), {'rebased onto' if rebased else 'already on'} main, tested in a throwaway VM).",
           ""] + closes + fixes_ghsa + vuln_lines
if notes:
    summary += ["", "Notes:"] + [f"- {n}" for n in notes]
title = sh("git", "log", "-1", "--format=%s", own[0]).strip()   # the branch's first own commit
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
head = sh("git", "rev-parse", "HEAD").strip()
push = subprocess.run(["git", *git_auth(), "push", "-q", f"--force-with-lease=refs/heads/{branch}:{current[0] if current else ''}",
                       f"https://github.com/{REPO}.git", f"HEAD:refs/heads/{branch}"], capture_output=True, text=True)
if push.returncode:
    # A rejected push can still have landed (2026-11-01: "cannot lock ref ... reference already exists", yet the
    # branch held this exact commit). Continue only if GitHub's branch is exactly HEAD; otherwise fail as before.
    now = sh("git", *git_auth(), "ls-remote", f"https://github.com/{REPO}.git", f"refs/heads/{branch}").split()
    if not now or now[0] != head:
        raise SystemExit(redact(f"push to GitHub {branch} failed: {push.stderr.strip()[-800:]}"))
    print(f"NOTE: push reported a failure, but GitHub {branch} is exactly {head[:7]}; continuing: "
          + redact(push.stderr.strip().splitlines()[-1] if push.stderr.strip() else "?"))
owner = REPO.split("/")[0]
existing = gh(f"/repos/{REPO}/pulls?head={owner}:{branch}&state=open") or []
if existing:
    pr = gh(f"/repos/{REPO}/pulls/{existing[0]['number']}", "PATCH", {"title": title, "body": body})
else:
    pr = gh(f"/repos/{REPO}/pulls", "POST", {"title": title, "head": branch, "base": "main", "body": body})
print(f"\nPR: {pr.get('html_url')}")
