#!/usr/bin/env python3
"""
build_kinetic_app.py

USAGE:
  python build_kinetic_app.py <folder1> [folder2] ...

EXAMPLES:
  python build_kinetic_app.py MyApp
  python build_kinetic_app.py Apps/MyApp AnotherApp

DESCRIPTION:
  • For each passed folder, ensures it starts with "Apps/".  
  • Recursively finds every *.json or *.jsonc file.  
  • Preserves the entire "Apps\..." path inside the ZIP archive.  
  • Creates a timestamped .zip in bin\ with a name like "MyApp_2025-01-28-135454.zip".  
  • Skips any non-existent folder.  

REQUIREMENTS:
  • Python 3.x  
  • Standard library "zipfile", "os", and "datetime".  

NOTES:  
  • If you have no *.json/*.jsonc files, the resulting .zip could be empty.  
  • Adjust as needed (e.g. to include more file types, or to handle additional logic).  
"""

import os
import sys
import zipfile
from datetime import datetime

def main():
    print("DEBUG: Entered main()")  # Add this to confirm script startup
    if len(sys.argv) < 2:
        print("Usage: python build_kinetic_app.py <AppFolderOrPath> [...]")
        sys.exit(1)

    # Ensure bin\ directory exists
    os.makedirs("bin", exist_ok=True)

    # Base "Apps" folder in absolute form
    apps_base = os.path.abspath("Apps")  # e.g., "C:/Path/To/Project/Apps"

    for raw_path in sys.argv[1:]:
        # Normalize so that it starts with "Apps/" if not already
        if not raw_path.strip().lower().startswith("apps"):
            source_path = os.path.join("Apps", raw_path)
        else:
            source_path = raw_path

        source_folder = os.path.abspath(source_path)

        if not os.path.isdir(source_folder):
            print(f"[WARNING] Folder not found, skipping: {source_folder}")
            continue

        # Example: "Ice.UIDbd.ProductionTracker"
        app_base_name = os.path.basename(os.path.normpath(source_folder))

        # Build a timestamped ZIP name in bin\
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        zip_filename = f"{app_base_name}_{timestamp}.zip"
        zip_path = os.path.join("bin", zip_filename)

        print()
        print("-------------------------------------------------------------")
        print(f"Building '{source_folder}' => '{zip_path}' (only *.json, *.jsonc)")
        print("-------------------------------------------------------------")

        # Open or create the .zip file fresh
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Walk everything under the specified folder
            for root, dirs, files in os.walk(source_folder):
                for fname in files:
                    # Include *.json or *.jsonc
                    lower_name = fname.lower()
                    if lower_name.endswith(".json") or lower_name.endswith(".jsonc"):
                        full_path = os.path.join(root, fname)
                        # Preserve path starting at the folder ABOVE "Apps" so that
                        # inside the ZIP we see: Apps\AppFolder\sub\file.jsonc
                        arcname = os.path.relpath(full_path, start=os.path.dirname(apps_base))
                        print(f"  + {arcname}")
                        zf.write(full_path, arcname=arcname)

        print(f"Created: {zip_path}")

    print("\nAll requested folders have been processed.")

if __name__ == "__main__":
    main()