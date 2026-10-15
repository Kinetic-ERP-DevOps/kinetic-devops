import os
import base64
import binascii
import re
import argparse
import sys
import subprocess
import zipfile
import io
from typing import List, Tuple, Dict, Any

try:
    from .auth import KineticConfigManager
except ImportError:
    KineticConfigManager = None

# Define generic patterns for sensitive information
GENERIC_PATTERNS = {
    # Look for assignments to variables named like api_key, secret, etc.
    "ASSIGNMENT_SECRET": re.compile(r'(?i)\b(api_key|api_secret|access_token|secret_key)\s*[:=]\s*["\']([^"\']{8,})["\']'),
    # JWTs are fairly distinct (start with ey...)
    "JWT_TOKEN": re.compile(r'["\'](ey[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*)["\']'),
    # Generic long hex strings often used for keys (32+ chars)
    "GENERIC_HEX_32": re.compile(r'["\']([a-fA-F0-9]{32,})["\']'),
    # Generic Base64-like strings often used for keys/secrets.
    # Final reporting is filtered through heuristics to suppress identifier noise.
    "GENERIC_BASE64_32": re.compile(r'["\']([A-Za-z0-9+/_-]{24,}={0,2})["\']'),
    "PRIVATE_KEY_BLOCK": re.compile(r'PRIVATE KEY'),  # kd-sensitive-scan-ignore-line: pattern definition, not a secret
}

CAMEL_CASE_RE = re.compile(r'^(?:[A-Z][a-z0-9]+)+(?:[A-Z][a-z0-9]+)*$|^[a-z]+(?:[A-Z][a-z0-9]+)+$')
SNAKE_CASE_RE = re.compile(r'^[A-Za-z]+(?:_[A-Za-z0-9]+)+$')
UPPER_UNDERSCORE_RE = re.compile(r'^[A-Z0-9]+(?:_[A-Z0-9]+)+$')
DASH_SEPARATED_RE = re.compile(r'^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$')
PATH_LIKE_RE = re.compile(r'^[A-Za-z0-9./-]+$')
REPEATED_PUNCTUATION_RE = re.compile(r'^[-_=+/]{16,}$')

# Exclusion Lists
WINDOWS_EXCLUDE_DIRS = {"$RECYCLE.BIN", "System Volume Information"}
BUILD_EXCLUDE_DIRS = {"node_modules", "dist", "build", "venv", ".venv", "env", "__pycache__", "bin", "obj"}
METADATA_EXCLUDE_DIRS = {".git", ".vs", ".vscode", ".idea"}
CUSTOM_EXCLUDE_DIRS = {"schemas", "templates", "reports", "Apps", "layers", "temp", "BusyBox-c695", "Win-c695"}

# Combine all exclusions
DEFAULT_EXCLUDE_DIRS = set().union(
    WINDOWS_EXCLUDE_DIRS, 
    BUILD_EXCLUDE_DIRS, 
    METADATA_EXCLUDE_DIRS, 
    CUSTOM_EXCLUDE_DIRS
)

# File extensions to ignore (Binary / Media)
BINARY_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.bmp', '.tiff',
    '.exe', '.dll', '.so', '.dylib', '.bin',
    '.pyc', '.pyo', '.pyd', '.class',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx',
    '.zip', '.tar', '.gz', '.7z', '.rar'
}


class ScanError(RuntimeError):
    """Raised when a requested scan could not complete, so callers can fail closed."""


def _repo_relative_include_path(start_path: str, include_path: str) -> str:
    """Normalize an include path against the git repo root.

    Git diff/log/show output paths are always repo-relative, but callers may pass
    ``include_path`` as an absolute path or one relative to the current working
    directory. Resolving it against the repo root keeps ``_path_is_included``
    matching real scan output instead of silently filtering everything out.
    """
    normalized = _normalize_path_for_match(include_path)
    if normalized in {"", "."}:
        return ""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=start_path, capture_output=True, text=True, check=True,
        )
        repo_root = result.stdout.strip()
        if not repo_root:
            return normalized
        abs_include = os.path.abspath(include_path)
        rel = os.path.relpath(abs_include, repo_root)
        if rel.startswith(".."):
            return normalized
        return _normalize_path_for_match(rel)
    except Exception:
        return normalized


def _normalize_path_for_match(path: str) -> str:
    """Normalize paths so exclude matching works across Windows/posix styles."""
    value = str(path or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/").lower()


def _normalize_exclude_tokens(exclude_dirs: set) -> set:
    normalized = set()
    for item in exclude_dirs:
        norm = _normalize_path_for_match(item)
        if norm:
            normalized.add(norm)
    return normalized


def _path_is_excluded(path: str, exclude_tokens: set) -> bool:
    norm_path = _normalize_path_for_match(path)
    if not norm_path:
        return False
    parts = [p for p in norm_path.split("/") if p]
    if any(part in exclude_tokens for part in parts):
        return True
    return any(norm_path.startswith(f"{token}/") or norm_path == token for token in exclude_tokens)


def _path_is_included(path: str, include_token: str) -> bool:
    """Return True when a path falls under the explicit scan scope."""
    if not include_token:
        return True
    norm_path = _normalize_path_for_match(path)
    return norm_path == include_token or norm_path.startswith(f"{include_token}/")

def is_text_file(file_path: str) -> bool:
    """Check if a file is text (not binary) by extension and content."""
    # Fast check by extension
    _, ext = os.path.splitext(file_path)
    if ext.lower() in BINARY_EXTENSIONS:
        return False
        
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(1024)
            return b'\x00' not in chunk
    except Exception:
        return False


def _is_camel_case_token(value: str) -> bool:
    return bool(CAMEL_CASE_RE.fullmatch(str(value or "")))


def _is_snake_case_token(value: str) -> bool:
    token = str(value or "")
    return bool(SNAKE_CASE_RE.fullmatch(token) or UPPER_UNDERSCORE_RE.fullmatch(token))


def _is_dash_separated_token(value: str) -> bool:
    return bool(DASH_SEPARATED_RE.fullmatch(str(value or "")))


def _is_path_like_token(value: str) -> bool:
    token = str(value or "")
    return "/" in token and bool(PATH_LIKE_RE.fullmatch(token))


def _decode_base64_candidate(value: str) -> bytes | None:
    token = str(value or "").strip()
    if not token:
        return None

    normalized = token.replace("-", "+").replace("_", "/")
    remainder = len(normalized) % 4
    if remainder == 1:
        return None
    if remainder:
        normalized += "=" * (4 - remainder)

    try:
        return base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError):
        return None


def _should_report_generic_base64(
    value: str,
    include_camel_case: bool = False,
    include_snake_case: bool = False,
    include_dash_separated: bool = False,
    include_path_like: bool = False,
    include_alpha_only: bool = False,
    min_decoded_bytes: int = 12,
) -> bool:
    token = str(value or "").strip()
    if len(token) < 24:
        return False

    is_camel = _is_camel_case_token(token)
    is_snake = _is_snake_case_token(token)
    is_dash = _is_dash_separated_token(token)
    is_path_like = _is_path_like_token(token)
    alpha_only = token.isalpha()
    has_digit = any(ch.isdigit() for ch in token)
    has_base64_symbols = any(ch in "+/=_-" for ch in token)

    if REPEATED_PUNCTUATION_RE.fullmatch(token):
        return False

    if is_camel and not include_camel_case:
        return False
    if is_snake and not include_snake_case:
        return False
    if is_dash and not include_dash_separated:
        return False
    if is_path_like and not include_path_like:
        return False
    if alpha_only and not include_alpha_only and not (is_camel and include_camel_case) and not (is_snake and include_snake_case):
        return False
    if not has_digit and not has_base64_symbols and not include_alpha_only and not (is_camel and include_camel_case) and not (is_snake and include_snake_case):
        return False
    if not has_digit and ("-" in token or "/" in token) and not ("+" in token or "=" in token or "_" in token) and not include_dash_separated and not include_path_like:
        return False

    decoded = _decode_base64_candidate(token)
    if decoded is None:
        return False
    return len(decoded) >= max(1, int(min_decoded_bytes))


# Inline marker for narrowly suppressing a known false positive on a single line
# (e.g. a pattern definition or test fixture that looks like a secret but isn't).
# Unlike a path-level --exclude, this only silences the one annotated line, so a
# real secret added elsewhere in the same file is still caught. This only works
# for the current working tree / new commits though: it can't retroactively
# annotate lines already committed to history. For git-history/diff/commit/stash
# scans, KNOWN_PATTERN_FALSE_POSITIVES below covers already-committed false
# positives by (pattern, path) instead, so a real secret of any *other* pattern
# type in these files is still caught.
SUPPRESS_MARKER = "kd-sensitive-scan-ignore-line"

KNOWN_PATTERN_FALSE_POSITIVES: Dict[str, set] = {
    "PRIVATE_KEY_BLOCK": {
        "src/kinetic_devops/find_sensitive_data.py",
        "kinetic_devops/find_sensitive_data.py",  # path before the src/ layout, still in history
        "tests/test_sensitive_data_scan_exclusions.py",
    },
}


def _is_known_pattern_false_positive(pattern_name: str, path: str) -> bool:
    paths = KNOWN_PATTERN_FALSE_POSITIVES.get(pattern_name)
    if not paths:
        return False
    normalized = _normalize_path_for_match(path)
    return any(normalized == p or normalized.endswith(f"/{p}") for p in paths)


def _collect_line_matches(
    line: str,
    patterns: Dict[str, re.Pattern],
    generic_base64_options: Dict[str, Any] | None = None,
    path: str = "",
) -> List[Tuple[str, str]]:
    if SUPPRESS_MARKER in line:
        return []
    findings: List[Tuple[str, str]] = []
    options = generic_base64_options or {}

    for pattern_name, regex in patterns.items():
        if path and _is_known_pattern_false_positive(pattern_name, path):
            continue
        for match in regex.finditer(line):
            if pattern_name == "GENERIC_BASE64_32":
                candidate = match.group(1) if match.lastindex else match.group(0).strip("\"'")
                if not _should_report_generic_base64(candidate, **options):
                    continue
            findings.append((pattern_name, match.group(0)))
    return findings

def scan_zip_archive(
    zip_path: str,
    patterns: Dict[str, re.Pattern],
    generic_base64_options: Dict[str, Any] | None = None,
) -> List[Tuple[str, int, str, str]]:
    """Scans files inside a zip archive."""
    findings = []
    try:
        if not zipfile.is_zipfile(zip_path):
            return findings

        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                
                # Skip common binary extensions inside archives
                if member.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.exe', '.dll', '.pyc', '.class')):
                    continue
                
                # Skip very large files inside archives (e.g. > 5MB)
                if member.file_size > 5 * 1024 * 1024:
                    continue

                try:
                    with zf.open(member) as f:
                        text_file = io.TextIOWrapper(f, encoding='utf-8', errors='ignore')
                        for i, line in enumerate(text_file, 1):
                            if len(line) > 500: continue
                            for pattern_name, matched_text in _collect_line_matches(line, patterns, generic_base64_options, path=member.filename):
                                findings.append((f"{zip_path}!{member.filename}", i, pattern_name, matched_text))
                except Exception:
                    pass
    except Exception:
        pass
    return findings

def get_sensitive_data_from_keyring() -> Dict[str, re.Pattern]:
    """
    Retrieves sensitive data from the keyring and creates regex patterns.
    """
    patterns = {}
    if not KineticConfigManager:
        return patterns

    try:
        mgr = KineticConfigManager()
        patterns = mgr.get_sensitive_data_patterns()
        if not patterns:
            print("  -> ⚠️  No configurations found in keyring. Only generic patterns will be used.")
            print("     Run 'python -m kinetic_devops.auth store' to add a server configuration.")
        return patterns
    except Exception as e:
        print(f"Could not get sensitive data patterns from auth manager: {e}")
        return patterns

def get_files_to_scan(start_path: str, use_gitignore: bool, exclude_dirs: set) -> List[str]:
    """Generates a list of files to scan, optionally respecting .gitignore."""
    file_list = []
    
    exclude_tokens = _normalize_exclude_tokens(exclude_dirs)

    normalized_start = _normalize_path_for_match(start_path)
    explicit_subpath = normalized_start not in {"", "."}

    # Method 1: Try Git (if requested and available). For explicit subpaths,
    # prefer direct filesystem walk so explicit path selection is honored.
    if use_gitignore and not explicit_subpath:
        try:
            # Check if git is installed and this is a repo
            subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], cwd=start_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            
            # List tracked and untracked files, respecting .gitignore
            cmd = ['git', 'ls-files', '-c', '-o', '--exclude-standard']
            result = subprocess.run(cmd, cwd=start_path, capture_output=True, text=True, check=True)
            
            for line in result.stdout.splitlines():
                if _path_is_excluded(line, exclude_tokens):
                    continue

                full_path = os.path.join(start_path, line)
                if os.path.isfile(full_path):
                    file_list.append(full_path)
            return file_list
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass # Fallback

    # Method 2: os.walk with manual exclusions
    for root, dirs, files in os.walk(start_path):
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith('.')]
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, start_path)
            if _path_is_excluded(rel_path, exclude_tokens):
                continue
            file_list.append(full_path)
            
    return file_list

def scan_git_history(
    start_path: str,
    patterns: Dict[str, re.Pattern],
    exclude_dirs: set,
    include_path: str = ".",
    generic_base64_options: Dict[str, Any] | None = None,
) -> List[Tuple[str, int, str, str]]:
    """Scans git history (patches) for sensitive data."""
    findings = []
    print("Scanning git history (this may take a moment)...")
    exclude_tokens = _normalize_exclude_tokens(exclude_dirs)
    include_token = _repo_relative_include_path(start_path, include_path)
    try:
        # Scan patches without context lines
        cmd = ['git', 'log', '-p', '--unified=0']
        process = subprocess.Popen(cmd, cwd=start_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        
        current_commit = "Unknown"
        current_file = "Unknown"
        for line in process.stdout:
            if line.startswith("commit "):
                current_commit = line.strip().split(" ")[1]
                continue
            if line.startswith("diff --git"):
                current_file = line.split(" b/")[-1].strip()
                continue
            
            # Only check added lines
            if not line.startswith('+'): continue
            if _path_is_excluded(current_file, exclude_tokens):
                continue
            if not _path_is_included(current_file, include_token):
                continue
                
            content = line[1:]
            for pattern_name, _ in _collect_line_matches(content, patterns, generic_base64_options, path=current_file):
                findings.append((f"COMMIT: {current_commit} ({current_file})", 0, pattern_name, content.strip()[:100]))
                    
        process.wait()
        if process.returncode != 0:
            stderr_output = process.stderr.read() if process.stderr else ""
            raise ScanError(f"git log exited {process.returncode}: {stderr_output}")
    except ScanError:
        raise
    except Exception as e:
        raise ScanError(f"Error scanning git history: {e}") from e
    return findings

def scan_git_diff(
    start_path: str,
    patterns: Dict[str, re.Pattern],
    exclude_dirs: set,
    staged: bool = False,
    include_path: str = ".",
    generic_base64_options: Dict[str, Any] | None = None,
) -> List[Tuple[str, int, str, str]]:
    """Scans git diffs (staged or unstaged) for sensitive data."""
    findings = []
    diff_type = "staged" if staged else "unstaged"
    print(f"Scanning {diff_type} git changes...")
    exclude_tokens = _normalize_exclude_tokens(exclude_dirs)
    include_token = _repo_relative_include_path(start_path, include_path)
    try:
        # Scan diffs without context lines to isolate added secrets
        cmd = ['git', 'diff', '--unified=0']
        if staged:
            cmd.append('--cached')
        
        process = subprocess.Popen(cmd, cwd=start_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        
        current_file = "Unknown"
        for line in process.stdout:
            if line.startswith("diff --git"):
                # Extract filename from: diff --git a/path/to/file b/path/to/file
                current_file = line.split(" b/")[-1].strip()
                continue
            
            # Only check added lines (+) that are not metadata (+++)
            if line.startswith('+') and not line.startswith('+++'):
                if _path_is_excluded(current_file, exclude_tokens):
                    continue
                if not _path_is_included(current_file, include_token):
                    continue
                content = line[1:]
                if len(content) > 500: continue
                for pattern_name, _ in _collect_line_matches(content, patterns, generic_base64_options, path=current_file):
                    findings.append((f"DIFF ({diff_type}): {current_file}", 0, pattern_name, content.strip()[:100]))
        process.wait()
        if process.returncode != 0:
            stderr_output = process.stderr.read() if process.stderr else ""
            raise ScanError(f"git diff exited {process.returncode}: {stderr_output}")
    except ScanError:
        raise
    except Exception as e:
        raise ScanError(f"Error scanning git diff: {e}") from e
    return findings

def scan_git_commit(
    start_path: str,
    commit_hash: str,
    patterns: Dict[str, re.Pattern],
    exclude_dirs: set,
    include_path: str = ".",
    generic_base64_options: Dict[str, Any] | None = None,
) -> List[Tuple[str, int, str, str]]:
    """Scans a specific git commit for sensitive data."""
    findings = []
    print(f"Scanning commit {commit_hash}...")
    exclude_tokens = _normalize_exclude_tokens(exclude_dirs)
    include_token = _repo_relative_include_path(start_path, include_path)
    try:
        cmd = ['git', 'show', '--unified=0', commit_hash]
        process = subprocess.Popen(cmd, cwd=start_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        
        current_file = "Unknown"
        for line in process.stdout:
            if line.startswith("diff --git"):
                current_file = line.split(" b/")[-1].strip()
                continue
            
            if line.startswith('+') and not line.startswith('+++'):
                if _path_is_excluded(current_file, exclude_tokens):
                    continue
                if not _path_is_included(current_file, include_token):
                    continue
                content = line[1:]
                if len(content) > 500: continue
                for pattern_name, _ in _collect_line_matches(content, patterns, generic_base64_options, path=current_file):
                    findings.append((f"COMMIT: {commit_hash[:7]} ({current_file})", 0, pattern_name, content.strip()[:100]))
        process.wait()
        if process.returncode != 0:
            stderr_output = process.stderr.read() if process.stderr else ""
            raise ScanError(f"Error scanning commit {commit_hash}: {stderr_output}")
    except ScanError:
        raise
    except Exception as e:
        raise ScanError(f"Error scanning commit {commit_hash}: {e}") from e
    return findings

def scan_git_stashes(
    start_path: str,
    patterns: Dict[str, re.Pattern],
    exclude_dirs: set,
    include_path: str = ".",
    generic_base64_options: Dict[str, Any] | None = None,
) -> List[Tuple[str, int, str, str]]:
    """Scans all git stashes for sensitive data."""
    findings = []
    print("Scanning git stashes...")
    exclude_tokens = _normalize_exclude_tokens(exclude_dirs)
    include_token = _repo_relative_include_path(start_path, include_path)
    try:
        stash_list_result = subprocess.run(['git', 'stash', 'list'], cwd=start_path, capture_output=True, text=True)
        if stash_list_result.returncode != 0:
            print("  No stashes found to scan.")
            return findings

        stashes = [line.split(':')[0] for line in stash_list_result.stdout.splitlines()]
        if not stashes:
            print("  No stashes found to scan.")
            return findings

        for stash_ref in stashes:
            print(f"  - Scanning {stash_ref}...")
            cmd = ['git', 'stash', 'show', '-p', '--unified=0', stash_ref]
            process = subprocess.Popen(cmd, cwd=start_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
            
            current_file = "Unknown"
            for line in process.stdout:
                if line.startswith("diff --git"):
                    current_file = line.split(" b/")[-1].strip()
                    continue
                
                if line.startswith('+') and not line.startswith('+++'):
                    if _path_is_excluded(current_file, exclude_tokens):
                        continue
                    if not _path_is_included(current_file, include_token):
                        continue
                    content = line[1:]
                    if len(content) > 500: continue
                    for pattern_name, _ in _collect_line_matches(content, patterns, generic_base64_options, path=current_file):
                        findings.append((f"STASH: {stash_ref} ({current_file})", 0, pattern_name, content.strip()[:100]))
            process.wait()
            if process.returncode != 0:
                stderr_output = process.stderr.read() if process.stderr else ""
                raise ScanError(f"git stash show {stash_ref} exited {process.returncode}: {stderr_output}")
    except ScanError:
        raise
    except Exception as e:
        raise ScanError(f"Error scanning git stashes: {e}") from e
    return findings

def find_sensitive_data(
    files: List[str],
    all_patterns: Dict[str, re.Pattern],
    generic_base64_options: Dict[str, Any] | None = None,
) -> List[Tuple[str, int, str, str]]:
    """Scans the provided list of files."""
    findings = []
    
    for file_path in files:
        if file_path.lower().endswith('.zip'):
            findings.extend(scan_zip_archive(file_path, all_patterns, generic_base64_options=generic_base64_options))
            continue

        if not is_text_file(file_path):
            continue
            
        try:
            with open(file_path, "r", encoding="utf-8", errors='ignore') as f:
                for i, line in enumerate(f, 1):
                    # Skip minified lines
                    if len(line) > 500: continue

                    for pattern_name, matched_text in _collect_line_matches(line, all_patterns, generic_base64_options, path=file_path):
                        findings.append((file_path, i, pattern_name, matched_text))
        except Exception:
            pass
    return findings


def _resolve_scan_files(scan_path: str, explicit_file: str, use_gitignore: bool, exclude_dirs: set) -> List[str]:
    target_file = str(explicit_file or "").strip()
    if target_file:
        if not os.path.isfile(target_file):
            return []
        exclude_tokens = _normalize_exclude_tokens(exclude_dirs)
        rel_path = os.path.basename(target_file)
        if _path_is_excluded(rel_path, exclude_tokens):
            return []
        return [target_file]

    if os.path.isfile(scan_path):
        exclude_tokens = _normalize_exclude_tokens(exclude_dirs)
        rel_path = os.path.basename(scan_path)
        if _path_is_excluded(rel_path, exclude_tokens):
            return []
        return [scan_path]

    return get_files_to_scan(scan_path, use_gitignore=use_gitignore, exclude_dirs=exclude_dirs)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scan for sensitive data in the project.\n"
            "Read-only scan of files (and optionally git history, diffs, a commit or stashes) for generic\n"
            "secret patterns, Base64 blobs, custom regexes and values derived from the saved Kinetic\n"
            "environments in the keyring (hostnames, API keys, company IDs). Findings are listed by file and\n"
            "line with the matched text redacted. Reading the keyring may prompt for the vault passphrase;\n"
            "use --no-keyring to skip it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.find_sensitive_data --path . --fail-on-findings\n"
            "  python -m kinetic_devops.find_sensitive_data --staged --fail-on-findings   # pre-commit style\n"
            "  python -m kinetic_devops.find_sensitive_data --file some_export.json --no-keyring"
        ),
    )
    parser.add_argument("--path", metavar="DIR", default=".", help="The directory path to scan (default: current directory).")
    parser.add_argument("--file", metavar="FILE", default="", help="A single file to scan instead of a directory.")
    parser.add_argument("--no-gitignore", action="store_true", help="Do not use .gitignore for file exclusion.")
    parser.add_argument("--ignore-defaults", action="store_true", help="Ignore the default exclusion lists (build, metadata, etc.).")
    parser.add_argument("--exclude", nargs='+', default=[], help="Additional directories to exclude.")

    # Pattern Control
    parser.add_argument("--no-keyring", action="store_true", help="Disable scanning for secrets from the keyring (enabled by default).")
    parser.add_argument("--no-generic-base64", action="store_true", help="Disable scanning for generic Base64 patterns.")
    parser.add_argument("--include-camel-case-base64", action="store_true", help="Include CamelCase and PascalCase tokens in generic Base64 matching.")  # kd-sensitive-scan-ignore-line: argparse flag name, not a secret
    parser.add_argument("--include-snake-case-base64", action="store_true", help="Include snake_case and UPPER_UNDERSCORE tokens in generic Base64 matching.")  # kd-sensitive-scan-ignore-line: argparse flag name, not a secret
    parser.add_argument("--include-dash-separated-base64", action="store_true", help="Include dash-separated identifier tokens in generic Base64 matching.")  # kd-sensitive-scan-ignore-line: argparse flag name, not a secret
    parser.add_argument("--include-path-like-base64", action="store_true", help="Include path-like slash-separated tokens in generic Base64 matching.")  # kd-sensitive-scan-ignore-line: argparse flag name, not a secret
    parser.add_argument("--include-alpha-only-base64", action="store_true", help="Include alphabetic-only tokens in generic Base64 matching.")  # kd-sensitive-scan-ignore-line: argparse flag name, not a secret
    parser.add_argument("--base64-min-decoded-bytes", type=int, default=12, help="Minimum decoded byte length required for generic Base64 findings.")
    parser.add_argument("--custom-pattern", nargs='+', default=[], help="Define one or more custom regex patterns to scan for.")

    # Git Scanning Modes
    parser.add_argument("--history", action="store_true", help="Also scan git history (commits).")
    parser.add_argument("--diff", action="store_true", help="Scan unstaged git changes.")
    parser.add_argument("--staged", action="store_true", help="Scan staged git changes.")
    parser.add_argument("--commit", help="Scan a specific git commit hash.")
    parser.add_argument("--git-stash", action="store_true", help="Scan all git stashes.")
    parser.add_argument("--fail-on-findings", action="store_true", help="Exit with code 1 when findings are detected.")
    args = parser.parse_args()

    # --- Build Patterns ---
    custom_patterns = {}
    if not args.no_keyring:
        print("Getting sensitive data from keyring...")
        custom_patterns.update(get_sensitive_data_from_keyring())
        print(f"Found {len(custom_patterns)} sensitive data patterns from keyring.")

    if args.custom_pattern:
        for i, pattern_str in enumerate(args.custom_pattern):
            try:
                custom_patterns[f"CUSTOM_PATTERN_{i+1}"] = re.compile(pattern_str)
                print(f"✅ Added custom pattern: {pattern_str}")
            except re.error as e:
                print(f"❌ Error compiling custom pattern '{pattern_str}': {e}")

    all_patterns = {**GENERIC_PATTERNS, **custom_patterns}
    generic_base64_options = {
        "include_camel_case": bool(args.include_camel_case_base64),
        "include_snake_case": bool(args.include_snake_case_base64),
        "include_dash_separated": bool(args.include_dash_separated_base64),
        "include_path_like": bool(args.include_path_like_base64),
        "include_alpha_only": bool(args.include_alpha_only_base64),
        "min_decoded_bytes": int(args.base64_min_decoded_bytes),
    }

    if args.no_generic_base64:
        all_patterns.pop("GENERIC_BASE64_32", None)
        print("ℹ️  Generic Base64 pattern scanning is disabled.")

    # --- Configure Exclusions ---
    if args.ignore_defaults:
        exclude_dirs = set(args.exclude)
    else:
        exclude_dirs = DEFAULT_EXCLUDE_DIRS.union(set(args.exclude))

    # --- Transparency Log ---
    print("\n--- Scan Configuration ---")
    if not args.no_gitignore:
        print("Mode: .gitignore is ENABLED (recursively respects all .gitignore files).")
    else:
        print("Mode: .gitignore is DISABLED.")
    
    if args.ignore_defaults:
        print("Default Exclusions: IGNORED.")
    
    if exclude_dirs:
        print(f"Manual Exclusions: {sorted(list(exclude_dirs))}")
    print("--------------------------\n")
    
    # --- Run Scans ---
    findings = []

    scan_target = args.file.strip() or args.path
    if args.file.strip():
        print(f"Gathering file {args.file}...")
    else:
        print(f"Gathering files in {args.path}...")

    files = _resolve_scan_files(args.path, args.file, use_gitignore=not args.no_gitignore, exclude_dirs=exclude_dirs)
    print(f"Scanning {len(files)} files...")
    findings.extend(find_sensitive_data(files, all_patterns, generic_base64_options=generic_base64_options))

    scan_failures = []

    def _run_git_scan(fn, *fn_args, **fn_kwargs):
        try:
            findings.extend(fn(*fn_args, **fn_kwargs))
        except ScanError as e:
            print(f"❌ {e}")
            scan_failures.append(str(e))

    if args.history:
        _run_git_scan(
            scan_git_history,
            args.path,
            all_patterns,
            exclude_dirs=exclude_dirs,
            include_path=args.path,
            generic_base64_options=generic_base64_options,
        )

    if args.diff:
        _run_git_scan(
            scan_git_diff,
            args.path,
            all_patterns,
            exclude_dirs=exclude_dirs,
            staged=False,
            include_path=args.path,
            generic_base64_options=generic_base64_options,
        )

    if args.staged:
        _run_git_scan(
            scan_git_diff,
            args.path,
            all_patterns,
            exclude_dirs=exclude_dirs,
            staged=True,
            include_path=args.path,
            generic_base64_options=generic_base64_options,
        )

    if args.commit:
        _run_git_scan(
            scan_git_commit,
            args.path,
            args.commit,
            all_patterns,
            exclude_dirs=exclude_dirs,
            include_path=args.path,
            generic_base64_options=generic_base64_options,
        )

    if args.git_stash:
        _run_git_scan(
            scan_git_stashes,
            args.path,
            all_patterns,
            exclude_dirs=exclude_dirs,
            include_path=args.path,
            generic_base64_options=generic_base64_options,
        )

    if scan_failures and args.fail_on_findings:
        print(f"\n❌ {len(scan_failures)} requested scan(s) could not complete; failing closed.")
        return 1

    # --- Report Findings ---
    # NOTICE_* patterns flag things worth reviewing (e.g. internal environment
    # nicknames) but never fail the scan.
    notices = [f for f in findings if str(f[2]).startswith("NOTICE_")]
    findings = [f for f in findings if not str(f[2]).startswith("NOTICE_")]
    if notices:
        print("\nNotices (review; these do not fail the scan):")
        for location, line_num, pattern, match in notices:
            loc_str = f"{location}:{line_num}" if line_num > 0 else location
            print(f"  - {loc_str} [{pattern}]\n    Match: <redacted, {len(match)} chars>")
    if findings:
        print("\nSensitive data found:")
        for location, line_num, pattern, match in findings:
            loc_str = f"{location}:{line_num}" if line_num > 0 else location
            print(f"  - {loc_str} [{pattern}]\n    Match: <redacted, {len(match)} chars>")
        if args.fail_on_findings:
            return 1
    else:
        print("\nNo sensitive data found.")

    # --- Final Tip ---
    git_scans_performed = args.history or args.diff or args.staged or args.commit or args.git_stash
    if not git_scans_performed:
        print("\nℹ️  Tip: To scan git history, use --history, --diff, --staged, --commit <hash>, or --git-stash.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
