"""ci-bot: decide whether this event is for ci-bot, then review and/or re-run tests.

Triggers (anything else exits quietly):
  pull_request review_requested, with ci-bot among the requested reviewers   -> review
  issue_comment on a PR mentioning @ci-bot, by a user with write access      -> review,
                                                     or "@ci-bot test" -> dispatch the test workflow
Never fails the job: problems are printed and reported back as a comment where possible.
"""
import json, os, re, subprocess, sys, urllib.error, urllib.request

ENV = os.environ
API, REPO, TOKEN = ENV["FORGEJO_API"].rstrip("/"), ENV["REPO"], ENV["FORGEJO_TOKEN"]
BOT = "ci-bot"


def api(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"token {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        return json.loads(data) if data else {}


def comment(pr, text):
    try:
        api("POST", f"/repos/{REPO}/issues/{pr}/comments", {"body": text})
    except Exception as e:
        print(f"ci-bot: could not comment: {e}")


def can_write(user):
    try:
        perm = api("GET", f"/repos/{REPO}/collaborators/{user}/permission").get("permission", "")
    except urllib.error.HTTPError:
        return False
    return perm in ("write", "admin", "owner")


def main():
    event = json.load(open(ENV["GITHUB_EVENT_PATH"]))
    name = ENV.get("EVENT_NAME", "")
    action = "review"
    if name == "pull_request":
        reviewers = [r.get("login") for r in (event.get("pull_request", {}).get("requested_reviewers") or [])]
        reviewers.append((event.get("requested_reviewer") or {}).get("login"))
        if BOT not in reviewers:
            print("ci-bot: review requested from someone else; nothing to do"); return
        pr = event["pull_request"]["number"]
        who = (event.get("sender") or {}).get("login", "?")
    elif name == "issue_comment":
        issue = event.get("issue", {})
        body = (event.get("comment") or {}).get("body", "")
        is_pr = issue.get("pull_request") or event.get("is_pull")
        if not is_pr or not re.search(rf"@{BOT}\b", body):
            print("ci-bot: not a PR comment for ci-bot"); return
        who = (event.get("comment") or {}).get("user", {}).get("login", "?")
        if who == BOT:
            return
        pr = issue["number"]
        if not can_write(who):
            print(f"ci-bot: {who} has no write access; ignored"); return
        if re.search(rf"@{BOT}\s+test\b", body, re.I):
            action = "test"
    else:
        print(f"ci-bot: event {name} not handled"); return

    info = api("GET", f"/repos/{REPO}/pulls/{pr}")
    head, base, sha = info["head"]["ref"], info["base"]["ref"], info["head"]["sha"]

    if action == "test":
        wf = ENV.get("TEST_WORKFLOW", "gate.yml")
        try:
            api("POST", f"/repos/{REPO}/actions/workflows/{wf}/dispatches", {"ref": head})
            comment(pr, f"ci-bot: re-running `{wf}` on `{head}` (asked by @{who}); see the Actions tab.")
        except Exception as e:
            comment(pr, f"ci-bot: could not start `{wf}` on `{head}`: {e}")
        return

    if not ENV.get("LLM_API_KEY") or not ENV.get("LLM_BASE_URL"):
        missing = " and ".join(n for n, v in (("the `LLM_API_KEY` secret", ENV.get("LLM_API_KEY")),
                                                ("the `LLM_BASE_URL` variable", ENV.get("LLM_BASE_URL"))) if not v)
        comment(pr, f"ci-bot: {missing} isn't set for this repo/org, so I can't review yet."); return
    # Review exactly the PR head against its base (the checkout may be the default branch on comment events).
    subprocess.run(["git", "fetch", "--no-tags", "origin", f"+refs/pull/{pr}/head:refs/ci-bot/pr", base], check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", "refs/ci-bot/pr"], check=True)
    env = dict(ENV, PR_NUMBER=str(pr), BASE_REF=base)
    print(f"ci-bot: reviewing PR #{pr} ({head} @ {sha[:10]}) against {base}, asked by @{who}")
    subprocess.run([sys.executable, ENV["REVIEW_SCRIPT"]], env=env)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ci-bot: {e}")
    sys.exit(0)
