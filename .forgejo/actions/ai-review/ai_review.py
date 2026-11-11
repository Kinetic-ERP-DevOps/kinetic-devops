"""Advisory diff review using a local OpenAI-compatible endpoint.

Never fails the build: any error is printed and the script exits 0.
"""
import json
import os
import socket
import subprocess
import sys
import threading
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


READ_TIMEOUT = 30  # seconds one recv may stall once answer content is flowing


def response_socket(r):
    return getattr(getattr(getattr(r, "fp", None), "raw", None), "_sock", None)  # HTTPResponse's socket


def bound_next_read(r, deadline, idle=None):
    """Past the deadline raise TimeoutError; else cap the next socket recv at the time left (and at
    `idle` once content flows). One readline() can be many recvs (bytes trickling without a newline),
    so this alone doesn't bound a whole read: deadline_watchdog does."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("review exceeded its deadline")
    sock = response_socket(r)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(min(left, idle) if idle else left)


class deadline_watchdog:
    """Shut the response's socket down at the deadline, so a trickling read ends there: the blocked
    recv returns EOF and the deadline check after it raises. shutdown() is safe across threads."""

    def __init__(self, r, deadline):
        self.sock, self.timer = response_socket(r), None
        if self.sock is not None and hasattr(self.sock, "shutdown"):
            self.timer = threading.Timer(max(deadline - time.monotonic(), 0.0), self.fire)
            self.timer.daemon = True

    def fire(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def __enter__(self):
        if self.timer:
            self.timer.start()
        return self

    def __exit__(self, *exc):
        if self.timer:
            self.timer.cancel()
        return False


def chat(url, payload, headers, deadline_s=900, idle_s=READ_TIMEOUT):
    """Streamed chat completion: giving up (deadline, error) closes the connection, which frees the
    server slot at once; an abandoned non-streaming request keeps the GPU busy until it finishes.
    The deadline bounds the whole answer, plain-JSON fallback included; once content flows (not on a
    blank, keep-alive or role-only line) a recv may stall at most idle_s. Stops at `data: [DONE]`."""
    req = urllib.request.Request(url, json.dumps(dict(payload, stream=True)).encode(), headers, method="POST")
    deadline, parts, idle = time.monotonic() + deadline_s, [], None
    with urllib.request.urlopen(req, timeout=deadline_s) as r, deadline_watchdog(r, deadline):
        while True:
            bound_next_read(r, deadline, idle)
            raw = r.readline()
            if time.monotonic() > deadline:
                raise TimeoutError("review exceeded its deadline")
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if line == "data: [DONE]" or line == "data:[DONE]":
                break
            if not line.startswith("data:"):
                if line.startswith("{"):  # server ignored stream: plain JSON, read under the same deadline
                    idle = idle_s
                    body = [raw]
                    while True:
                        bound_next_read(r, deadline, idle)
                        piece = r.read(65536)
                        if time.monotonic() > deadline:
                            raise TimeoutError("review exceeded its deadline")
                        if not piece:
                            break
                        body.append(piece)
                    return json.loads(b"".join(body).decode("utf-8", "replace"))["choices"][0]["message"]["content"]
                continue
            for c in json.loads(line[5:]).get("choices") or []:
                delta = c.get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                    idle = idle_s
                parts.append(delta.get("content") or "")
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
