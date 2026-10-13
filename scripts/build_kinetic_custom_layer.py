import json
import os
import sys
import re

def strip_jsonc_comments(jsonc_string):
    """
    Strips single-line and multi-line comments from a JSONC string.
    This is necessary to handle .jsonc files which are not valid JSON.
    """
    # Remove multi-line comments (/* ... */)
    stripped_string = re.sub(r'/\*[\s\S]*?\*/', '', jsonc_string)
    # Remove single-line comments (// ...)
    stripped_string = re.sub(r'//.*', '', stripped_string)
    return stripped_string

def embed_and_escape_content(input_file_path):
    """
    Reads a JSON file, escapes its content into a single-line string,
    and embeds it into the 'Content' key of a new file.

    Args:
        input_file_path (str): The path to the input JSON file to embed.

    Returns:
        str: A success or error message.
    """
    # Check if the file exists
    if not os.path.exists(input_file_path):
        return f"Error: The file '{input_file_path}' was not found."

    try:
        # Step 1: Read the content of the input file
        with open(input_file_path, 'r') as infile:
            raw_content = infile.read()

        # Step 2: Strip comments to handle .jsonc format
        clean_content = strip_jsonc_comments(raw_content)

        # Step 3: Parse the clean JSON to ensure it's valid
        json_data = json.loads(clean_content)

        # Step 4: Escape the content and make it a single-line string.
        # This will add escape characters and remove formatting.
        escaped_content = json.dumps(json_data)

        # Step 5: Create a new dictionary to hold the escaped content
        # We use a structure similar to your example for consistency.
        output_data = {
            "App": "ExampleApp",
            "Operation": "Merge",
            "TargetFile": f"/path/to/{os.path.basename(input_file_path)}",
            "Content": escaped_content
        }

        # Step 6: Define the output file path and write the new data
        base, ext = os.path.splitext(input_file_path)
        output_file_path = f"{base}_escaped{ext}"

        with open(output_file_path, 'w') as outfile:
            json.dump(output_data, outfile, indent=2)

        return f"Successfully created new file with embedded content: '{output_file_path}'"

    except json.JSONDecodeError as e:
        return f"Error: Failed to decode JSON from '{input_file_path}'. Details: {e}"

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python embed_and_escape.py <input_file_path>")
        sys.exit(1)
    
    input_file_path = sys.argv[1]
    result = embed_and_escape_content(input_file_path)
    print(result)
    if result.startswith("Error:"):
        sys.exit(1)
