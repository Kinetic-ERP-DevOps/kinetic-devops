"""Suggest a commit message for the staged changes using the stored AI profile.

Uses the same AI server settings as `kinetic_devops.analysis` (`auth ai-set`: URL, API style,
model, API key, review budget), so any local model the profile points at works; the staged
diff is cut to the profile's max_diff_chars (default sized for small models).

    uv run python scripts/generate-commit.py [--ai-base-url URL]
"""
import argparse
import subprocess

from kinetic_devops import analysis


def get_staged_changes() -> str:
    """Retrieve staged changes using `git diff --cached`."""
    try:
        result = subprocess.run(["git", "diff", "--cached"], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print("Error retrieving staged changes:", e)
        return ""


def build_prompt(changes: str) -> str:
    return (
        "Generate a concise commit message for these changes. Follow these rules:\n"
        "- Imperative mood (e.g., 'Fix bug')\n"
        "- Conventional prefix: type(scope): summary\n"
        "- Max 50 characters for title\n"
        "- Reference related issues (if applicable)\n"
        "- Keep the scope focused\n"
        "Reply with the commit message only.\n\n"
        f"Staged diff:\n{changes}"
    )


def generate_commit_message(changes: str, base_url: str = "", timeout: int = 120) -> str:
    url = analysis._resolve_ai_base_url(base_url)
    budget = analysis.resolve_ai_budget(url)
    models = analysis.fetch_models(url, timeout)
    if not models:
        raise RuntimeError("No AI models available")
    model = analysis.select_model(models, url)
    prompt = build_prompt(analysis.truncate_text(changes, budget["max_diff_chars"]))
    response = analysis.call_ai(url, timeout, model, prompt, max_tokens=min(200, budget["max_output_tokens"]))
    return analysis.extract_text(response)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ai-base-url", default="", metavar="URL", help="AI server (default: stored AI profile)")
    args = parser.parse_args()
    changes = get_staged_changes()
    if changes:
        print("Generated Commit Message:\n" + generate_commit_message(changes, args.ai_base_url))
    else:
        print("No staged changes found.")
