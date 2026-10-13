import os
import argparse
import fnmatch

def is_text_file(filepath):
    """
    Determines if a file is not text by checking for null bytes and attempting to decode it as UTF-8.
    
    Args:
        filepath (str): Path to the file to check.
    
    Returns:
        bool: True if the file is a text file, False if it is binary.
    """
    try:
        with open(filepath, 'rb') as file:
            chunk = file.read(1024)
            if b'\x00' in chunk:
                return False  # Binary file
            chunk.decode('utf-8')
        return True
    except (UnicodeDecodeError, IOError):
        return False

def get_excluded_items(default_exclusions, user_exclusions, replace=False):
    """
    Combines default and user-provided exclusions.
    """
    if replace:
        return set(user_exclusions)
    return set(default_exclusions).union(user_exclusions)

def merge_files(input_dir, output_file, file_types, excluded_dirs, excluded_files, include_hidden, replace_exclusions):
    excluded_dirs = get_excluded_items(DEFAULT_EXCLUDED_DIRS, excluded_dirs, replace_exclusions)
    excluded_files = get_excluded_items(DEFAULT_EXCLUDED_FILES, excluded_files, replace_exclusions)
    excluded_items_report = []

    header_comment = (
        "# Merged Text Files\n"
        "# These files are included for reference purposes.\n\n"
    )

    with open(output_file, 'w', encoding='utf-8', newline='\n') as outfile:
        outfile.write(header_comment)
        outfile.write("# Excluded Items:\n")

        for root, dirs, files in os.walk(input_dir):
            # Exclude hidden directories unless include_hidden is True
            if not include_hidden:
                dirs[:] = [d for d in dirs if not d.startswith('.')]

            # Exclude specified directories
            for d in dirs[:]:
                if d in excluded_dirs:
                    excluded_items_report.append(f"Excluded directory: {os.path.join(root, d)}")
                    dirs.remove(d)

            for filename in sorted(files):
                file_path = os.path.join(root, filename)

                # Skip the output file
                if os.path.abspath(file_path) == os.path.abspath(output_file):
                    excluded_items_report.append(f"Excluded output file: {file_path}")
                    continue

                # Skip excluded files
                if filename in excluded_files:
                    excluded_items_report.append(f"Excluded file: {file_path}")
                    continue

                # Skip files not matching the specified file types
                if file_types and not any(filename.endswith(ext) for ext in file_types):
                    excluded_items_report.append(f"Excluded by file type: {file_path}")
                    continue

                # Check if the file is a text file
                if is_text_file(file_path):
                    print(f"Processing file: {file_path}")
                    outfile.write(f"# File: {os.path.relpath(file_path, input_dir)}\n")
                    outfile.write(f"# {'-' * 80}\n\n")

                    # Read and write the file content
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as infile:
                        content = infile.read()
                        normalized_content = content.replace('\r\n', '\n').replace('\r', '\n')
                        outfile.write(normalized_content)

                    outfile.write(f"\n\n# {'=' * 80}\n\n")
                else:
                    excluded_items_report.append(f"Excluded binary file: {file_path}")

        # Write excluded items report
        outfile.write("\n# Excluded Items Report:\n")
        for item in excluded_items_report:
            outfile.write(f"# {item}\n")

    print(f"Merged files have been written to {output_file}")

if __name__ == "__main__":
    # Default exclusions
    DEFAULT_EXCLUDED_DIRS = {
        'venv', '.git', '.cache', 'scrap', '.config', '.local', 'local_models', 'node_modules', '__pycache__', 'langchain', 'open-interpreter'
    }
    DEFAULT_EXCLUDED_FILES = {'README.md', 'LICENSE'}

    # Argument parser setup
    parser = argparse.ArgumentParser(description="Merge text files from a directory into a single file.")
    parser.add_argument('input_dir', type=str, help="Input directory to scan for text files.")
    parser.add_argument('output_file', type=str, help="Path to the output merged file.")
    parser.add_argument('-e', '--exclude-dirs', nargs='*', default=[], help="Additional directories to exclude.")
    parser.add_argument('-f', '--exclude-files', nargs='*', default=[], help="Additional files to exclude.")
    parser.add_argument('-t', '--file-types', nargs='*', default=[], help="File types to include (e.g., *.py, *.txt).")
    parser.add_argument('-a', '--include-hidden', action='store_true', help="Include hidden directories.")
    parser.add_argument('-r', '--replace-exclusions', action='store_true', help="Replace default exclusions with user-provided ones.")
    args = parser.parse_args()

    # Merge files based on provided arguments
    merge_files(
        input_dir=args.input_dir,
        output_file=args.output_file,
        file_types=args.file_types,
        excluded_dirs=args.exclude_dirs,
        excluded_files=args.exclude_files,
        include_hidden=args.include_hidden,
        replace_exclusions=args.replace_exclusions
    )