import subprocess
import ollama  # Ensure the `ollama` Python package is installed via `pip install ollama`

def get_staged_changes():
    """Retrieve staged changes using `git diff --cached`."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()  # Return the diff output
    except subprocess.CalledProcessError as e:
        print("Error retrieving staged changes:", e)
        return ""

def generate_commit_message(changes_description):
    """Generate a commit message using ikamen/phi3.5mini:Q4KM via Ollama."""
    # Create an Ollama client instance
    client = ollama.Client()

    # Craft the prompt with best practices
    prompt = (
        "Generate a concise commit message for these changes: "
        f"{changes_description}. Follow these rules:\n"
        "- Imperative mood (e.g., 'Fix bug')\n"
        "- Max 50 characters for title\n"
        "- Reference related issues (if applicable)\n"
        "- Keep the scope focused"
    )

    # Generate the message (streaming)
    response = client.generate(
        model="ikamen/phi3.5mini:Q4KM",
        prompt=prompt,
        stream=False,  # Set to `True` if you want streaming output
    )

    return response["response"]

if __name__ == "__main__":
    changes = get_staged_changes()
    if changes:
        message = generate_commit_message(changes)
        print("Generated Commit Message:\n" + message)
    else:
        print("No staged changes found.")