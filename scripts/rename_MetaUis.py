import os
import zipfile
import re
import json
from datetime import datetime
import time
import argparse

# --- Utility Functions (extract_part1, parse_and_format_date, etc. unchanged) ---

def extract_part1(folder_name):
    """
    Extracts Part1 (the last segment of a dot-separated folder name).
    e.g., 'Erp.UI.PartEntry' -> 'PartEntry'
    """
    return folder_name.split('.')[-1] if '.' in folder_name else folder_name

def parse_and_format_date(date_string):
    """
    Attempts to parse common date formats and returns a formatted date string (YYYY-MM-DD).
    """
    KNOWN_FORMATS = [
        '%Y-%m-%dT%H:%M:%SZ',    
        '%Y-%m-%dT%H:%M:%S',     
        '%m/%d/%Y %I:%M:%S %p',  
        '%m/%d/%Y %H:%M:%S',     
        '%m/%d/%Y',              
        '%Y-%m-%d',              
    ]

    for fmt in KNOWN_FORMATS:
        try:
            dt_obj = datetime.strptime(date_string.strip(), fmt)
            return dt_obj.strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None

def extract_content_version(json_content):
    """
    Extracts the 'Version' field from a Layer's JSON content and formats it as a date.
    """
    version_value = None
    
    # 1. Regex search for "Version": "..."
    match = re.search(r'"Version"\s*:\s*"([^"]*)"', json_content)
    if match:
        version_value = match.group(1)
    
    # 2. JSON parsing as fallback (handles comments in JSONC)
    if not version_value:
        try:
            # Remove single-line and multi-line comments for safe parsing
            clean_content = re.sub(r'//.*', '', json_content)
            clean_content = re.sub(r'/\*[\s\S]*?\*/', '', clean_content)
            data = json.loads(clean_content)
            if 'Version' in data and isinstance(data['Version'], str):
                version_value = data['Version']
        except json.JSONDecodeError:
            pass

    if version_value:
        return parse_and_format_date(version_value)
        
    return None

def resolve_filename_conflict(proposed_filename_ext, target_dir, original_filename_ext):
    """
    Checks for file existence and appends an index (1), (2), etc., to resolve conflicts.
    """
    is_conflict = (proposed_filename_ext != original_filename_ext and 
                   os.path.exists(os.path.join(target_dir, proposed_filename_ext)))

    if not is_conflict:
        return proposed_filename_ext, False 

    name, ext = os.path.splitext(proposed_filename_ext)
    index = 1
    while True:
        indexed_name_ext = f"{name} ({index}){ext}"
        full_path_check = os.path.join(target_dir, indexed_name_ext)

        # Safety check: if the indexed name somehow matches the original, we stop indexing
        if indexed_name_ext == original_filename_ext:
            return original_filename_ext, False 

        if not os.path.exists(full_path_check):
            return indexed_name_ext, True 

        index += 1

# --- Core Logic Function (Modified) ---

def process_zip_file(zip_path, target_dir, auto_rename, use_short_name):
    """
    Detects the zip file structure, extracts parts, resolves conflicts, and optionally prompts for rename.
    The 'use_short_name' flag determines if the App/Layer name uses the full key (default) or the last segment.
    """
    original_filename_ext = os.path.basename(zip_path)
    print(f"\n--- Processing {original_filename_ext} ---")

    new_zip_path = None
    new_filename_ext = None
    rename_ready = False
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_file:
            namelist = zip_file.namelist()

            # Regex for Layers structure (e.g., Layers/Erp.UI.PartEntry/MyLayer.jsonc)
            layers_match_re = re.compile(r'^Layers/([^/]+)/([^/]+\.jsonc)$', re.IGNORECASE)
            layers_members = [member for member in namelist if layers_match_re.match(member)]

            # Regex for Apps structure (e.g., Apps/Erp.UI.HourlyDashboard/layout.jsonc)
            apps_match_re = re.compile(r'^Apps/([^/]+)/(.+\.jsonc)$', re.IGNORECASE)
            apps_members = [member for member in namelist if apps_match_re.match(member)]

            if layers_members:
                # Layers Structure Logic (3-Part Rename)
                match = layers_match_re.match(layers_members[0])
                layer_folder_name_full = match.group(1) # e.g., Erp.UI.PartEntry
                jsonc_file_member = layers_members[0]
                
                # Part1: Screen Key (default is long, short is optional)
                if use_short_name:
                    part1 = extract_part1(layer_folder_name_full)
                    print(f"  - Detected Structure: Layers (3-Part Rename - Short App Key, requested by --short-name)")
                else:
                    part1 = layer_folder_name_full
                    print(f"  - Detected Structure: Layers (3-Part Rename - Full App Key, default)")
                
                # Part2: Layer Name (e.g., MyLayer)
                part2 = os.path.splitext(match.group(2))[0]
                
                jsonc_content = zip_file.read(jsonc_file_member).decode('utf-8')
                # Part3: Version Date (YYYY-MM-DD)
                part3 = extract_content_version(jsonc_content)
                
                if not part3:
                    print(f"  Skipping: Could not extract and format 'Version' date (Part3) from layer file: {jsonc_file_member}.")
                    return

                new_filename_ext = f"{part1} {part2} {part3}.zip"
                

            elif apps_members:
                # Apps Structure Logic (2-Part Rename)
                match = apps_match_re.match(apps_members[0])
                app_folder_name_full = match.group(1) # e.g., Erp.UI.HourlyDashboard
                
                # Part1: App Key (default is long, short is optional)
                if use_short_name:
                    part1 = extract_part1(app_folder_name_full)
                    print(f"  - Detected Structure: Apps (2-Part Rename - Short Key, requested by --short-name)")
                else:
                    part1 = app_folder_name_full
                    print(f"  - Detected Structure: Apps (2-Part Rename - Full Key, default)")
                
                # Part3: Most recent file date
                latest_date = None
                for member_name in apps_members:
                    info = zip_file.getinfo(member_name)
                    file_dt = datetime(*info.date_time)
                    if latest_date is None or file_dt > latest_date:
                        latest_date = file_dt

                if not latest_date:
                    print(f"  Skipping: Could not determine most recent file date in App structure.")
                    return

                part3 = latest_date.strftime('%Y-%m-%d')
                
                new_filename_ext = f"{part1} {part3}.zip"
                
            else:
                print(f"  Skipping: '{original_filename_ext}' does not match 'Layers' or 'Apps' structure.")
                return

        # 2. Rename Logic checks
        
        # Resolve potential file conflicts by indexing (e.g., adding (1))
        final_new_filename_ext, was_conflict = resolve_filename_conflict(new_filename_ext, target_dir, original_filename_ext)
        final_new_zip_path = os.path.join(target_dir, final_new_filename_ext)
        
        # FINAL CHECK: If the final resolved name is the same as the original name, skip.
        if original_filename_ext == final_new_filename_ext:
            if was_conflict: 
                print(f"  Skipping: File name '{original_filename_ext}' is already the best conflict-resolved name.")
            else:
                 print(f"  Skipping: Filename already matches the proposed name.")
            return

        if was_conflict:
            print(f"  ⚠️ CONFLICT: Proposed name '{new_filename_ext}' already exists on disk.")
        
        new_filename_ext = final_new_filename_ext
        new_zip_path = final_new_zip_path
        rename_ready = True
        
        # 3. RENAME EXECUTION
        if rename_ready:
            print("\n  RENAME PROPOSAL:")
            print(f"  Original Filename: {original_filename_ext}")
            print(f"  New Filename: {new_filename_ext}")
            
            should_rename = False
            if auto_rename:
                print("  ✅ Automatically confirming rename (--auto-rename flag is set).")
                should_rename = True
            else:
                response = input(f"  Do you want to rename '{original_filename_ext}' to '{new_filename_ext}'? (y/n): ")
                if response.strip().lower() == 'y':
                    should_rename = True
            
            if should_rename:
                try:
                    time.sleep(0.1) 
                    os.rename(zip_path, new_zip_path)
                    print(f"  ✅ SUCCESS: Renamed to {new_filename_ext}")
                except Exception as e:
                    print(f"  🛑 RENAME FAILED: An error occurred during rename: {e}")
            elif not auto_rename:
                print("  Rename skipped by user.")

    except zipfile.BadZipFile:
        print(f"  Skipping: '{original_filename_ext}' is not a valid zip file.")
    except Exception as e:
        print(f"  An unexpected error occurred while processing '{original_filename_ext}': {e}")


def main():
    """
    Main function to parse arguments and iterate over all zip files.
    """
    parser = argparse.ArgumentParser(description="Epicor Zip File Renamer Utility. Scans zip files for UI metadata and renames them based on contents.")
    
    # Optional positional argument for the directory path
    parser.add_argument(
        'target_dir',
        nargs='?', 
        default=os.getcwd(), 
        help="The directory path containing the zip files to process (defaults to current directory if not provided)."
    )

    parser.add_argument(
        '--auto-rename', 
        action='store_true', 
        help="Skips the interactive confirmation prompt and automatically renames files."
    )
    
    # New argument for controlling the name length
    parser.add_argument(
        '--short-name', 
        action='store_true', 
        help="For Apps/Layers, use the short name (e.g., 'PartEntry') instead of the full key (e.g., 'Erp.UI.PartEntry')."
    )
    
    args = parser.parse_args()

    TARGET_DIR = args.target_dir 

    print(f"*** Zip File Renamer Utility ***")
    print(f"Scanning directory: {TARGET_DIR}")
    if args.auto_rename:
        print("--- RUNNING IN AUTO-RENAME MODE (NO PROMPT) ---")
    
    if not os.path.exists(TARGET_DIR):
        print(f"Error: Target directory not found at {TARGET_DIR}")
        return

    # Filter for valid zip files
    zip_files = [f for f in os.listdir(TARGET_DIR) if f.lower().endswith('.zip') and os.path.isfile(os.path.join(TARGET_DIR, f))]
    
    if not zip_files:
        print("No zip files found in the directory.")
        return

    for filename in sorted(zip_files): 
        zip_path = os.path.join(TARGET_DIR, filename)
        # Pass both arguments to the processing function
        process_zip_file(zip_path, TARGET_DIR, args.auto_rename, args.short_name)

    print("\n*** Script finished processing all zip files. ***")

if __name__ == "__main__":
    main()