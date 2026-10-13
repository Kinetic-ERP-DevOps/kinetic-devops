import os
import subprocess

LOST_FOUND_DIR = "./git_lost+found"

def setup_lost_found_directory():
    """
    Ensure the `git_lost+found` directory exists, contains a .gitkeep file, and is ignored by Git.
    """
    if not os.path.exists(LOST_FOUND_DIR):
        os.makedirs(LOST_FOUND_DIR)
        print(f"Created directory: {LOST_FOUND_DIR}")

    # Add a .gitkeep file to ensure the directory is tracked
    gitkeep_path = os.path.join(LOST_FOUND_DIR, ".gitkeep")
    if not os.path.exists(gitkeep_path):
        with open(gitkeep_path, "w") as f:
            f.write("")  # Create an empty .gitkeep file
        print(f"Added .gitkeep file to {LOST_FOUND_DIR}")

    # Add the directory to .gitignore
    gitignore_path = ".gitignore"
    with open(gitignore_path, "a+") as gitignore:
        gitignore.seek(0)
        gitignore_content = gitignore.read()
        if LOST_FOUND_DIR not in gitignore_content:
            gitignore.write(f"\n{LOST_FOUND_DIR}\n")
            print(f"Added {LOST_FOUND_DIR} to .gitignore")

def get_dangling_objects():
    """
    Get a list of dangling blobs and trees from `git fsck`.
    """
    try:
        result = subprocess.run(["git", "fsck"], capture_output=True, text=True, check=True)
        lines = result.stdout.splitlines()
        dangling_objects = [
            (line.split()[1], line.split()[-1])  # (type, hash)
            for line in lines if "dangling" in line
        ]
        return dangling_objects
    except subprocess.CalledProcessError as e:
        print(f"Error running git fsck: {e.stderr}")
        return []

def save_object_to_file(obj_type, obj_hash):
    """
    Save the content of a dangling object (blob or tree) to a file in `git_lost+found`.
    """
    try:
        # Get the content of the object
        result = subprocess.run(
            ["git", "cat-file", "-p", obj_hash],
            capture_output=True,
            text=True,
            check=True
        )
        content = result.stdout

        # Determine the file path
        file_extension = "txt" if obj_type == "blob" else "tree"
        file_path = os.path.join(LOST_FOUND_DIR, f"{obj_hash}.{file_extension}")

        # Save the content to the file
        with open(file_path, "w") as f:
            f.write(content)
        print(f"Saved {obj_type} {obj_hash} to {file_path}")
    except subprocess.CalledProcessError as e:
        print(f"Error saving {obj_type} {obj_hash}:")
        print(f"  Command: {' '.join(e.cmd)}")
        print(f"  Return Code: {e.returncode}")
        print(f"  Stdout: {e.stdout.strip()}")
        print(f"  Stderr: {e.stderr.strip()}")

def main():
    """
    Main function to process dangling blobs and trees.
    """
    print("Setting up lost+found directory...")
    setup_lost_found_directory()

    print("Finding dangling objects...")
    dangling_objects = get_dangling_objects()

    if not dangling_objects:
        print("No dangling objects found.")
        return

    print(f"Found {len(dangling_objects)} dangling objects.")
    for obj_type, obj_hash in dangling_objects:
        print(f"Processing {obj_type} {obj_hash}...")
        save_object_to_file(obj_type, obj_hash)

    print("All dangling objects have been processed and saved.")

if __name__ == "__main__":
    main()