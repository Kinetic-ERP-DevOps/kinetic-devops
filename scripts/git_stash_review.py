#!/usr/bin/env python3
import subprocess
import sys
import os
import tempfile

# Helper to run a shell command and return output

def run(cmd, cwd=None, capture_output=True):
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=capture_output, text=True)
    if result.returncode != 0:
        if capture_output:
            print(result.stderr)
        raise Exception(f"Command failed: {cmd}")
    return result.stdout.strip() if capture_output else None

# Get list of stashes (oldest to newest)
def get_stash_list():
    out = run('git stash list')
    lines = out.splitlines()
    stashes = [line.split(':')[0] for line in lines]
    return stashes[::-1]  # bottom-up

# Show diff for a stash
def show_stash_diff(stash):
    try:
        diff = run(f"git stash show -p '{stash}'")
    except Exception as e:
        raise Exception(f"Could not show diff for {stash}: {e}")
    return diff

# Get files changed in a stash
def get_stash_files(stash):
    out = run(f"git diff --name-only '{stash}^3' '{stash}^2'")
    return out.splitlines()

# Restore specific files from a stash
def restore_files_from_stash(stash, files):
    for f in files:
        run(f"git checkout '{stash}^3' -- '{f}'", capture_output=False)

# Main interactive loop
def main():
    stashes = get_stash_list()
    if not stashes:
        print("No stashes found.")
        return
    for stash in stashes:
        print(f"\n===== Reviewing {stash} =====")
        try:
            diff = show_stash_diff(stash)
        except Exception as e:
            print(f"[Warning] Could not show diff for {stash}: {e}\nSkipping.")
            continue
        print(diff)
        while True:
            action = input("Action? [k]eep, [d]rop, [p]op, [c]herrypick, [r]estore: ").strip().lower()
            if action == 'k':
                break
            elif action == 'd':
                try:
                    run(f"git stash drop '{stash}'", capture_output=False)
                    print(f"Dropped {stash}")
                except Exception as e:
                    print(f"[Warning] Could not drop {stash}: {e}")
                break
            elif action == 'p':
                try:
                    run(f"git stash pop '{stash}'", capture_output=False)
                    print(f"Popped {stash}")
                except Exception as e:
                    print(f"[Warning] Could not pop {stash}: {e}")
                break
            elif action == 'r':
                try:
                    run(f"git stash apply '{stash}'", capture_output=False)
                    print(f"Restored {stash} (kept in stash)")
                except Exception as e:
                    print(f"[Warning] Could not restore {stash}: {e}")
                break
            elif action == 'c':
                try:
                    files = get_stash_files(stash)
                except Exception as e:
                    print(f"[Warning] Could not get files for {stash}: {e}")
                    continue
                if not files:
                    print("No files to cherrypick.")
                    continue
                print("Files in stash:")
                for idx, f in enumerate(files):
                    print(f"  {idx+1}: {f}")
                sel = input("Enter file numbers to restore (comma separated): ")
                try:
                    nums = [int(x.strip()) for x in sel.split(',') if x.strip()]
                    chosen = [files[i-1] for i in nums if 1 <= i <= len(files)]
                except Exception:
                    print("Invalid selection.")
                    continue
                if not chosen:
                    print("No files selected.")
                    continue
                for f in chosen:
                    print(f"\n--- Diff for {f} ---")
                    try:
                        diff = run(f"git diff '{stash}^3' '{stash}^2' -- '{f}'")
                        print(diff)
                    except Exception:
                        print("(No diff available)")
                confirm = input(f"Restore selected files? [y/N]: ").strip().lower()
                if confirm == 'y':
                    try:
                        restore_files_from_stash(stash, chosen)
                        print(f"Restored: {', '.join(chosen)}")
                    except Exception as e:
                        print(f"[Warning] Could not restore files: {e}")
                    break
            else:
                print("Invalid action.")

if __name__ == "__main__":
    main()
