"""RepoMaker modular CLI entrypoint.

Usage:
  python -m kinetic_devops.repomaker apply [args...]
  python -m kinetic_devops.repomaker reposmith [args...]
  python -m kinetic_devops.repomaker smoke [args...]
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from kinetic_devops.repomaker import apply
from kinetic_devops.repomaker import reposmith

# command -> (one-line help, description). Each command's own options are
# parsed by its module, so everything after the command name is passed
# through untouched (including -h, which shows that command's options).
COMMANDS = {
    "apply": (
        "Apply branch protection to the repos listed in a targets config JSON (dry-run unless --apply).",
        "Reads the target repos/branches and protection rules from a config JSON and, with --apply, "
        "creates or overwrites the branch protection rule on each GitHub/Forgejo target. Without "
        "--apply it is a dry-run.",
    ),
    "reposmith": (
        "Smoke test: create a temporary repo, protect and verify it, then delete it (dry-run unless --apply).",
        "End-to-end check of the provider APIs: creates a temporary repository, applies and verifies "
        "branch protection, then deletes the repository. Without --apply it is a dry-run.",
    ),
    "smoke": ("Alias for reposmith.", "Alias for reposmith."),
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repomaker",
        description="RepoMaker modular CLI: branch-protection automation for GitHub and Forgejo repositories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run 'repomaker <command> -h' for that command's options.\n\n"
            "examples:\n"
            "  repomaker apply --config targets.json            # dry-run\n"
            "  repomaker apply --config targets.json --apply\n"
            "  repomaker reposmith --apply"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", title="commands")
    for name, (help_text, description) in COMMANDS.items():
        subparsers.add_parser(name, help=help_text, description=description, add_help=False)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    parser = _build_parser()
    if not args or args[0] in {"-h", "--help"}:
        parser.print_help()
        return 0

    command = str(args[0]).strip().lower()
    if command not in COMMANDS:
        print(f"Unknown RepoMaker command: {args[0]}", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 2
    parser.parse_args([command])
    remainder: List[str] = args[1:]

    if command == "apply":
        return apply.main(remainder)
    return reposmith.main(remainder)


if __name__ == "__main__":
    raise SystemExit(main())
