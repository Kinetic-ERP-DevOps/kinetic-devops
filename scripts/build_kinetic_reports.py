#!/usr/bin/env python3
"""
build_kinetic_reports.py

Author: K Barrow
Date: 2025-02-27
Version: 1.4 (Enforcing Exclusive Arguments and Batch Logic)

USAGE:
  # 1. Sequential Append (Your preferred method - clearest control)
  python build_kinetic_reports.py PackingSlip --output-name Reports.zip 
  python build_kinetic_reports.py ShippingLabels --append --output-name Reports.zip 

  # 2. Batch Build (Multiple reports into one monolithic ZIP)
  # R1 will create the ZIP, R2/R3 will append to it.
  python build_kinetic_reports.py ReportA ReportB ReportC --output-name Monolithic.zip
  
  # 3. Batch Build (Multiple reports into separate, timestamped ZIPs)
  python build_kinetic_reports.py ReportA ReportB

  # 4. Single Report with Custom Internal Name (Uses --report-name)
  python build_kinetic_reports.py ./reports/OldSlip --report-name NewSlipName

DESCRIPTION:
  • Processes *.rdl files in the specified folder(s).
  • Supports the --append flag for adding reports to an existing zip file.
  • **CRITICAL RULE:** --report-name can ONLY be used when packaging a single source path.
  • Searches for report folders across multiple predefined locations.
"""

import os
import sys
import zipfile
from datetime import datetime
import argparse

# --- Utility Functions (find_report_folder remains unchanged) ---

def find_report_folder(report_name):
    """
    Finds the folder for the given report name in the appropriate directories.

    Args:
        report_name (str): The name of the report folder to find.

    Returns:
        str: The path to the report folder if found, or None if not found.
    """
    # Define search locations
    search_locations = [
        os.path.abspath(report_name),  # ./ReportName
        os.path.abspath(os.path.join("reports", report_name)),  # ./reports/ReportName
        os.path.abspath(os.path.join("reports", "CustomReports", report_name)),  # ./reports/CustomReports/ReportName
    ]

    # Find all matching folders
    matches = [path for path in search_locations if os.path.isdir(path)]

    if len(matches) == 0:
        return None  # No matches found
    elif len(matches) == 1:
        return matches[0]  # Only one match found
    else:
        # Prompt the user to select a folder if multiple matches are found
        print(f"Multiple folders found for '{report_name}':")
        for i, match in enumerate(matches, start=1):
            print(f"  {i}. {match}")
        while True:
            try:
                choice = int(input("Select the folder to use (enter the number): "))
                if 1 <= choice <= len(matches):
                    return matches[choice - 1]
            except ValueError:
                pass
            print("Invalid input. Please enter a valid number.")

# --- Core Packaging Function ---

def package_reports(source_path, output_folder, output_name, report_name_override, custom=True, append=False):
    """
    Packages all .rdl files in the specified directory or file path into a zip file.

    Args:
        source_path (str): The path to the reports folder or file.
        output_folder (str): The folder where the zip file will be saved.
        output_name (str): The name of the zip file (None if creating a timestamped file).
        report_name_override (str): The name to use inside the zip (or None to derive it).
        custom (bool): Whether to include "CustomReports" in the zip path.
        append (bool): If True, append to the zip file; otherwise, create/overwrite.
    """
    # Normalize the source path
    source_path = os.path.abspath(source_path)

    # Ensure the source path exists
    if not os.path.exists(source_path):
        print(f"[WARNING] Path not found, skipping: {source_path}")
        return

    is_dir = os.path.isdir(source_path)
    
    # Determine the name of the report for the ZIP path
    if report_name_override:
        report_name = report_name_override
    else:
        # Derive the report name from the source folder/parent folder
        if is_dir:
            report_name = os.path.basename(os.path.normpath(source_path))
        else: # Single file, use parent folder name
            report_name = os.path.basename(os.path.dirname(source_path))

    # --- Determine Output File Name ---
    
    # If output_name is NOT provided AND we're NOT appending, generate a timestamped name.
    if not output_name and not append:
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        output_name = f"{report_name}_{timestamp}.zip"
    elif not output_name and append:
        # This case is blocked in main(), but included here for completeness
        print("[ERROR] Cannot append without specifying --output-name. Aborting.")
        return

    # Ensure the output folder exists
    os.makedirs(output_folder, exist_ok=True)

    # Full path to the zip file
    zip_path = os.path.join(output_folder, output_name)

    # Determine ZIP mode: 'a' for append, 'w' for write (new/overwrite)
    zip_mode = "a" if append else "w"
    action = "Appending to" if append else "Creating new"

    print()
    print("-------------------------------------------------------------")
    print(f"{action} archive '{zip_path}' (Mode: '{zip_mode}')")
    print(f"Adding reports from '{source_path}' (as '{report_name}')")
    print("-------------------------------------------------------------")

    # Determine the base path inside the zip file
    base_path = f"reports/CustomReports/{report_name}" if custom else f"reports/{report_name}"

    files_added = 0
    try:
        with zipfile.ZipFile(zip_path, mode=zip_mode, compression=zipfile.ZIP_DEFLATED) as zf:
            
            # Helper to add files from a directory walk
            def add_files_from_dir(directory):
                nonlocal files_added
                for root, _, files in os.walk(directory):
                    for fname in files:
                        if fname.lower().endswith(".rdl"):
                            full_path = os.path.join(root, fname)
                            
                            # 1. Get modification time (timestamp)
                            mtime = os.path.getmtime(full_path)
                            # 2. Format timestamp string (Using datetime.fromtimestamp)
                            timestamp_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                            
                            # Create relative path starting from the source_path
                            arcname = os.path.join(base_path, os.path.relpath(full_path, start=directory))
                            # Normalize path separators for archive
                            arcname = arcname.replace(os.path.sep, "/")
                            
                            # 3. Print the timestamp along with the archive name
                            print(f" [{timestamp_str}] + {arcname}")
                            # --- END OF MODIFICATION ---
                            
                            zf.write(full_path, arcname=arcname)
                            files_added += 1

            if is_dir:
                add_files_from_dir(source_path)
            elif source_path.lower().endswith(".rdl"):
                # Single .rdl file

                # 1. Get modification time (timestamp)
                mtime = os.path.getmtime(source_path)
                # 2. Format timestamp string (Using datetime.fromtimestamp)
                timestamp_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                # --- END OF MODIFICATION ---

                arcname = os.path.join(base_path, os.path.basename(source_path))
                arcname = arcname.replace(os.path.sep, "/")
                
                # 3. Print the timestamp along with the archive name
                print(f" [{timestamp_str}] + {arcname}")
                
                zf.write(source_path, arcname=arcname)
                files_added += 1
            else:
                print(f"[WARNING] Skipping file, not an RDL: {source_path}")

    except Exception as e:
        # Original: 
        print(f"[ERROR] Failed to write to zip file '{zip_path}': {e}")
        # current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # print(f"[ERROR] [{current_time}] Failed to write to zip file '{zip_path}': {e}")

        return

    # Original: print(f"Completed {action.lower()}: {zip_path}. Added {files_added} file(s).")
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{current_time}] Completed {action.lower()}: {zip_path}. Added {files_added} file(s).")

# --- Main Function (Updated for Exclusivity) ---

def main():
    parser = argparse.ArgumentParser(description="Package .rdl files into a zip archive.")
    parser.add_argument("paths", nargs="+", help="Paths to the report folders or files (e.g., 'PackingSlip' or '.\reports\PackingSlip').")
    
    # General Options
    parser.add_argument("--output-folder", default="bin", help="Folder to save the zip file (default: bin/).")
    parser.add_argument("--output-name", help="Name of the zip file (REQUIRED when using --append or for batching into one file).")
    parser.add_argument("--report-name", help="Name of the report (default: derived from folder name). CANNOT be used with multiple paths.")
    
    # Behavior Flags
    parser.add_argument("--notcustom", action="store_true", help="Build reports in 'reports/ReportName' instead of 'reports/CustomReports/ReportName'.")
    parser.add_argument("--append", action="store_true", help="Appends reports to an existing zip file instead of creating/overwriting.")

    args = parser.parse_args()

    # --- Validation ---
    
    # 1. Enforce Mutual Exclusivity: --report-name and multiple paths
    if args.report_name and len(args.paths) > 1:
        print("[ERROR] The --report-name argument cannot be used when packaging multiple report paths.")
        print("Remove --report-name, and each report will use its folder name inside the ZIP.")
        sys.exit(1)

    # 2. Require output name for append mode
    if args.append and not args.output_name:
        print("[ERROR] The --append flag requires specifying an output file name with --output-name. Aborting.")
        sys.exit(1)
        
    # --- Processing the Paths ---
    
    for i, path in enumerate(args.paths):
        # Determine if we should append for this path. 
        # It's true if --append was passed (sequential mode), 
        # OR if it's the 2nd+ path in a batch (i > 0) AND an output name was provided (monolithic mode).
        should_append = args.append or (i > 0 and args.output_name)
        
        # Resolve the path if it's just a name
        current_path = path
        if not os.path.exists(current_path):
            found_path = find_report_folder(current_path)
            if found_path:
                current_path = found_path
            else:
                print(f"[WARNING] Could not find folder for '{current_path}', skipping.")
                continue

        # Package the report
        # Note: args.report_name will only have a value if len(args.paths) == 1 due to validation check.
        package_reports(
            source_path=current_path,
            output_folder=args.output_folder,
            output_name=args.output_name, 
            report_name_override=args.report_name,
            custom=not args.notcustom,
            append=should_append
        )

    print("\nAll requested paths have been processed.")


if __name__ == "__main__":
    main()
