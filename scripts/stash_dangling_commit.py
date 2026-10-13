import subprocess

def get_dangling_commits():
    """
    Get a list of dangling commit hashes from `git fsck`.
    """
    try:
        result = subprocess.run(["git", "fsck"], capture_output=True, text=True, check=True)
        lines = result.stdout.splitlines()
        dangling_commits = [
            line.split()[-1] for line in lines if "dangling commit" in line
        ]
        return dangling_commits
    except subprocess.CalledProcessError as e:
        print(f"Error running git fsck: {e.stderr}")
        return []

def create_stash_from_commit(commit_hash):
    """
    Create a stash from a dangling commit using `git stash store`.
    """
    try:
        message = f"Recovered dangling commit {commit_hash}"
        result = subprocess.run(
            ["git", "stash", "store", "-m", message, commit_hash],
            capture_output=True, text=True, check=True
        )
        print(f"Successfully created stash for commit {commit_hash}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error creating stash for commit {commit_hash}:")
        print(f"  Command: {' '.join(e.cmd)}")
        print(f"  Return Code: {e.returncode}")
        print(f"  Stdout: {e.stdout.strip()}")
        print(f"  Stderr: {e.stderr.strip()}")
        return False

def handle_failure(commit_hash):
    """
    Prompt the user to retry, skip, or abort on failure.
    """
    while True:
        choice = input(f"Failed to create stash for commit {commit_hash}. Retry (r), Skip (s), or Abort (a)? ").strip().lower()
        if choice == 'r':
            return 'retry'
        elif choice == 's':
            return 'skip'
        elif choice == 'a':
            return 'abort'
        else:
            print("Invalid choice. Please enter 'r' to retry, 's' to skip, or 'a' to abort.")

def main():
    """
    Main function to convert all dangling commits into stashes.
    """
    print("Finding dangling commits...")
    dangling_commits = get_dangling_commits()
    
    if not dangling_commits:
        print("No dangling commits found.")
        return

    print(f"Found {len(dangling_commits)} dangling commits.")
    for commit_hash in dangling_commits:
        print(f"Processing commit {commit_hash}...")
        while True:
            success = create_stash_from_commit(commit_hash)
            if success:
                break
            else:
                action = handle_failure(commit_hash)
                if action == 'retry':
                    continue
                elif action == 'skip':
                    print(f"Skipping commit {commit_hash}.")
                    break
                elif action == 'abort':
                    print("Aborting process.")
                    return

if __name__ == "__main__":
    main()