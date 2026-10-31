"""Advisory diff review using a local OpenAI-compatible endpoint.

Never fails the build: any error is printed and the script exits 0.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

PROMPT = """You are a strict senior reviewer. Review this git diff.
Report only real problems, most severe first: bugs, security issues
(secrets, injection, unsafe deserialization), missing tests for changed
behavior, and breaking API changes. For each: file, line, one-sentence issue,
suggested fix. If nothing is wrong, say "No blocking issues." Be brief."""


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def post(url, payload, headers):
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def chat(url, payload, headers, deadline_s=900):
    """Streamed chat completion: giving up (deadline, error) closes the connection, which frees the
    server slot at once; an abandoned non-streaming request keeps the GPU busy until it finishes."""
    req = urllib.request.Request(url, json.dumps(dict(payload, stream=True)).encode(), headers, method="POST")
    deadline, parts = time.monotonic() + deadline_s, []
    with urllib.request.urlopen(req, timeout=deadline_s) as r:
        for line in r:
            if time.monotonic() > deadline:
                raise TimeoutError("review exceeded its deadline")
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                if line.startswith("{"):  # server ignored stream: plain JSON
                    return json.loads(line + r.read().decode())["choices"][0]["message"]["content"]
                continue
            for c in json.loads(line[5:]).get("choices") or []:
                parts.append((c.get("delta") or {}).get("content") or "")
    return "".join(parts)


def main():
    env = os.environ
    base = env.get("BASE_REF", "main")
    try:
        git("fetch", "--no-tags", "origin", base)
        merge_base = git("merge-base", "HEAD", f"origin/{base}").strip()
        diff = git("diff", "--stat", "--patch", merge_base, "HEAD")
    except subprocess.CalledProcessError as e:
        print(f"ai-review: cannot compute diff vs {base}: {e.stderr.strip()}")
        return
    if not diff.strip():
        print("ai-review: no changes vs", base)
        return
    limit = int(env.get("MAX_DIFF", "60000"))
    if len(diff) > limit:
        diff = diff[:limit] + "\n\n[diff truncated]"

    url = env["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    try:
        review = chat(url, {
            "model": env.get("LLM_MODEL", "local/fast"),
            "temperature": 0.1,
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": diff}],
        }, {"Authorization": f"Bearer {env['LLM_API_KEY']}", "Content-Type": "application/json"}).strip()
    except Exception as e:  # advisory only, but say so on the PR instead of staying silent
        print(f"ai-review: LLM call failed: {e}")
        pr = env.get("PR_NUMBER")
        if pr:
            try:
                post(f"{env['FORGEJO_API']}/repos/{env['REPO']}/issues/{pr}/comments",
                     {"body": f"ci-bot: the AI review failed (`{env.get('LLM_MODEL')}`): {e}"},
                     {"Authorization": f"token {env['FORGEJO_TOKEN']}", "Content-Type": "application/json"})
            except Exception as e2:
                print(f"ai-review: could not post the failure either: {e2}")
        return

    print("=" * 70, "\nLocal AI review (" + env.get("LLM_MODEL", "") + ")\n" + "=" * 70)
    print(review)

    pr = env.get("PR_NUMBER")
    if pr:
        try:
            post(f"{env['FORGEJO_API']}/repos/{env['REPO']}/issues/{pr}/comments",
                 {"body": f"### Local AI review (`{env.get('LLM_MODEL')}`)\n\n{review}"},
                 {"Authorization": f"token {env['FORGEJO_TOKEN']}", "Content-Type": "application/json"})
        except Exception as e:
            print(f"ai-review: could not post PR comment: {e}")


if __name__ == "__main__":
    main()
    sys.exit(0)
