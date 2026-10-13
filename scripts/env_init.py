#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import argparse
import secrets
import random
import tempfile
import time
from pathlib import Path
from typing import Optional, List

# Force UTF-8 encoding for Windows console (solves "charmap" codec errors)
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    if sys.stdout is not None:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Ensure the parent directory is in the path so we can find the kinetic_devops package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the manager from your library
try:
    from kinetic_devops import KineticConfigManager
except ImportError:
    print("Error: Could not find 'kinetic_devops'. Ensure you are running from the project root.", file=sys.stderr)
    sys.exit(1)

class KineticEnvManager:
    def __init__(self, env_nickname: str = None, db_override: str = None):
        # 1. ESTABLISH ROOT: Go up one level from /scripts/env_init.py
        self.root_dir = Path(__file__).resolve().parent.parent
        # 2. DEFINE TEMP FILES: Use per-run randomized files in OS temp dir
        self.temp_dir = Path(tempfile.gettempdir()) / "kinetic_env_init"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        self.temp_file = self.temp_dir / f"env_vars_{token}.bat"
        self.ps1_file = self.temp_dir / f"env_vars_{token}.ps1"
        self.sh_file = self.temp_dir / f"env_vars_{token}.sh"
        self.env_nickname = env_nickname
        self.mgr = KineticConfigManager()
        # 3. SQLite DB location logic
        self.default_db_path = os.path.join(str(self.root_dir), "bin", "sdk-config")
        self.db_path = db_override or os.environ.get("KINETIC_TAXCONFIG_DB") or self.default_db_path

    def cleanup_stale_temp_files(self, max_age_seconds: int = 24 * 3600):
        """Best-effort cleanup for stale temp files from interrupted runs."""
        if not self.temp_dir.exists():
            return

        now = time.time()

        for pattern in ("env_vars_*.bat", "env_vars_*.ps1", "env_vars_*.sh"):
            for item in self.temp_dir.glob(pattern):
                try:
                    age = int(now - item.stat().st_mtime)
                    if age > max_age_seconds:
                        item.unlink(missing_ok=True)
                except Exception:
                    pass

    def get_env_commands(self) -> List[str]:
        """Resolves the environment and generates the shell 'set' commands."""
        # 1. Resolution Logic
        target = self.env_nickname or "dev"
        base_cfg = self.mgr.get_base_config(target)

        # 2. If not found, trigger the interactive menu
        if not base_cfg:
            if self.env_nickname:
                print(f"ℹ️ Environment '{self.env_nickname}' not recognized.")
            
            # FIX: prompt_for_env returns (env, user, company) 
            # We unpack all three to avoid the 'too many values to unpack' error.
            self.env_nickname, _, _ = self.mgr.prompt_for_env(passive=True) 
            base_cfg = self.mgr.get_base_config(self.env_nickname) 

        if not base_cfg:
            raise ValueError(f"Could not resolve configuration for {self.env_nickname}")

        # 3. Build Commands
        commands = []
        prefix = "set " if os.name == 'nt' else "export "

        # Only emit values consumed by downstream scripts.
        commands.append(f"{prefix}KIN_COMPANY={base_cfg['companies']}")
        commands.append(f"{prefix}KIN_ENV_NAME={base_cfg['nickname']}")

        # Ensure PYTHONPATH points to repo root so helper scripts can import the SDK
        repo_root = str(self.root_dir).replace('\\', '/') if os.name != 'nt' else str(self.root_dir)
        commands.append(f"{prefix}PYTHONPATH={repo_root}")

        # Set KINETIC_TAXCONFIG_DB for SQLite config location
        commands.append(f"{prefix}KINETIC_TAXCONFIG_DB={self.db_path}")

        return commands

    def secure_wipe_and_delete(self, filepath: Optional[str] = None):
        """Stochastic wipe with per-loop jitter and random passes for the temp file."""
        target = filepath or self.temp_file 
        if not os.path.exists(target):
            return True

        try:
            file_size = os.path.getsize(target)
            passes = random.randint(3, 6)
            with open(target, "ba+", buffering=0) as f:
                for _ in range(passes):
                    pass_jitter = random.uniform(1.1, 2.1)
                    buffer_size = int(file_size * pass_jitter)
                    f.seek(0)
                    f.write(secrets.token_bytes(buffer_size))
                    f.flush()
                    os.fsync(f.fileno()) 
            os.remove(target)
            return True
        except Exception as e:
            print(f"⚠️ Wipe failed: {e}", file=sys.stderr)
            return False

def main():
    parser = argparse.ArgumentParser(description="Kinetic Environment Initializer Helper")
    parser.add_argument("env", nargs="?", default=None, help="Nickname of the environment")
    parser.add_argument("--cleanup-only", action="store_true", help="Wipe the temp .bat file and exit")
    parser.add_argument("--cleanup-path", action="append", default=[], help="Explicit temp file path to wipe; may be repeated")
    parser.add_argument("--taxconfig-db", default=None, help="Override path for KINETIC_TAXCONFIG_DB (SQLite config DB)")
    args, _ = parser.parse_known_args()

    env_manager = KineticEnvManager(args.env, db_override=args.taxconfig_db)
    env_manager.cleanup_stale_temp_files()

    if args.cleanup_only:
        success = True
        paths = [p for p in args.cleanup_path if p]
        if not paths:
            paths = [str(env_manager.temp_file), str(env_manager.ps1_file), str(env_manager.sh_file)]
        for item in paths:
            if not env_manager.secure_wipe_and_delete(item):
                success = False
        sys.exit(0) if success else sys.exit(1)

    try:
        commands = env_manager.get_env_commands()
        target_path = env_manager.temp_file 
        
        if os.name == 'nt':
            # Write to the temp file that the .bat script will call
            with open(target_path, "w") as f:
                f.write("@echo off\n")
                for cmd in commands:
                    f.write(f"{cmd}\n")
            # The .bat script looks for this specific string to know it succeeded
            print(f"WRITTEN_TO={target_path}")
            # Also write a PowerShell-friendly env file so PowerShell users can dot-source it.
            ps1_path = env_manager.ps1_file
            try:
                with open(ps1_path, "w", encoding="utf-8") as pf:
                    pf.write("# Generated by scripts/env_init.py - PowerShell env file\n")
                    for cmd in commands:
                        # commands are like 'set KEY=val' on Windows; convert to Set-Item
                        if cmd.lower().startswith('set '):
                            kv = cmd[4:]
                            if '=' in kv:
                                k, v = kv.split('=', 1)
                                # Escape single quotes in value
                                v_esc = v.replace("'", "''")
                                pf.write(f"Set-Item -Path Env:{k} -Value '{v_esc}'\n")
                print(f"WRITTEN_PS1={ps1_path}")
            except Exception:
                pass
        else:
            # Unix-like: write exports to a secure, caller-owned-only temp
            # file and emit its path, instead of printing commands for the
            # caller to eval. eval-ing arbitrary stdout is an injection risk
            # if any exported value ever contains shell metacharacters.
            sh_path = env_manager.sh_file
            fd = os.open(str(sh_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as sf:
                for cmd in commands:
                    sf.write(f"{cmd}\n")
            print(f"WRITTEN_SH={sh_path}")
                
    except Exception as e:
        print(f"PYTHON CRASH: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()