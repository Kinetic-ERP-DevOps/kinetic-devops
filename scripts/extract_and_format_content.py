import json
import sys
import os

def extract_and_format_content(input_file_path):
    """
    Reads a file with an escaped JSON string in the 'Content' key,
    and writes the unescaped and formatted content to a new .temp.jsonc file.
    
    Args:
        input_file_path (str): The path to the input JSON file.
    """
    # Check if the file exists
    if not os.path.exists(input_file_path):
        print(f"Error: The file '{input_file_path}' was not found.")
        sys.exit(1)

    try:
        # Define the output file path
        output_file_path = os.path.splitext(input_file_path)[0] + ".temp.jsonc"

        # Read the original file
        with open(input_file_path, 'r') as infile:
            data = json.load(infile)

        # Access the 'Content' key, which holds the escaped JSON string
        escaped_json_string = data.get('Content')

        if not escaped_json_string:
            print("Error: The 'Content' key was not found or is empty.")
            sys.exit(1)

        # Parse the inner escaped JSON string
        formatted_data = json.loads(escaped_json_string)

        # Write the pretty-printed JSON to the output file
        with open(output_file_path, 'w') as outfile:
            json.dump(formatted_data, outfile, indent=2)

        print(f"Successfully extracted and formatted content to '{output_file_path}'")

    except json.JSONDecodeError as e:
        print(f"Error: Failed to decode JSON. Check for syntax issues in '{input_file_path}'. Details: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python extract_and_format_content.py <input_file_path>")
        sys.exit(1)
    
    input_file_path = sys.argv[1]
    extract_and_format_content(input_file_path)