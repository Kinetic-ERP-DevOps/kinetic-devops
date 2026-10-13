import sys
import argparse
import os
from kinetic_devops import _lazy_import

keyring = _lazy_import("keyring")
from typing import Callable, Sequence
from .auth import main as auth_main
from .auth import KineticConfigManager
from .baq import main as baq_main
from .metafx import main as metafx_main
from .efx import main as efx_main
from .export_all import main as export_all_main
from .solutions import main as solutions_main
from .zdatatable import main as zdatatable_main
from .find_sensitive_data import main as find_sensitive_data_main
from .report_service import main as report_main
from .analysis import main as analysis_main
from .repomaker.__main__ import main as repomaker_main
from .access_scope import main as access_scope_main
import importlib.metadata


TOOLS: dict[str, Callable[[], object]] = {
    "auth": auth_main,
    "baq": baq_main,
    "meta": metafx_main,
    "export": export_all_main,
    "solutions": solutions_main,
    "zdatatable": zdatatable_main,
    "find": find_sensitive_data_main,
    "efx": efx_main,
    "report": report_main,
    "analysis": analysis_main,
    "repomaker": repomaker_main,
    "scope": access_scope_main,
}


def _normalize_session_id(value: str) -> str:
    token = str(value or "").strip().upper()
    if token.startswith("ID_"):
        token = token[3:]
    return token


def _resolve_last_session(mgr: KineticConfigManager) -> dict[str, str]:
    slot = keyring.get_password("KineticSDK", "LAST_GLOBAL_SESSION")
    if not slot:
        raise ValueError("No LAST_GLOBAL_SESSION pointer found")

    meta = mgr._get_token_meta(slot) or {}
    env_name = str(meta.get("env_name") or meta.get("nickname") or "").strip()
    user_id = str(meta.get("user_id") or "").strip()
    company_id = str(meta.get("current_company") or "").strip()

    # Backfill env/user from slot pattern: <nickname>-<user>-<hash>
    if not env_name or not user_id:
        try:
            parsed_env, parsed_user, _ = str(slot).rsplit("-", 2)
            env_name = env_name or parsed_env
            user_id = user_id or parsed_user
        except ValueError:
            pass

    servers = mgr._get_server_dict() or {}
    stored_env_name, cfg = mgr._find_env(servers, env_name)
    if stored_env_name:
        env_name = stored_env_name

    if cfg and not company_id:
        companies = [c.strip() for c in str(cfg.get("companies", "")).split(",") if c.strip()]
        if companies:
            company_id = companies[0]

    if not env_name or not user_id:
        raise ValueError("LAST_GLOBAL_SESSION is invalid or incomplete")

    return {
        "env": env_name,
        "user": user_id,
        "co": company_id,
    }


def _resolve_session_id(mgr: KineticConfigManager, requested_id: str) -> dict[str, str]:
    target = _normalize_session_id(requested_id)
    if not target:
        raise ValueError("--session-id cannot be empty")

    servers = mgr._get_server_dict() or {}
    matches: list[dict[str, str]] = []

    for env_name, cfg in servers.items():
        sessions = cfg.get("sessions", []) or []
        api_key = cfg.get("api_key", "")
        companies = [c.strip() for c in str(cfg.get("companies", "")).split(",") if c.strip()]
        for user_id in sessions:
            slot = mgr._get_token_key(env_name, user_id, api_key)
            suffix = slot[-4:].upper() if len(slot) >= 4 else ""
            if suffix != target:
                continue

            meta = mgr._get_token_meta(slot) or {}
            company_id = str(meta.get("current_company") or "").strip()
            if not company_id and companies:
                company_id = companies[0]

            matches.append({
                "env": str(env_name),
                "user": str(user_id),
                "co": company_id,
            })

    if not matches:
        raise ValueError(f"Session ID '{requested_id}' not found")
    if len(matches) > 1:
        raise ValueError(
            f"Session ID '{requested_id}' is ambiguous ({len(matches)} matches). "
            "Use explicit --env/--user in the subcommand."
        )

    return matches[0]


def _build_global_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--reuse-last-session",
        action="store_true",
        help="Reuse LAST_GLOBAL_SESSION for any subcommand that initializes a Kinetic session.",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="Target a specific saved session ID suffix (shown as ID_xxxx).",
    )
    parser.add_argument(
        "--tool-help",
        choices=sorted(TOOLS.keys()),
        default="",
        help="Show help for a specific tool parser while keeping tool options defined at the source.",
    )
    return parser


def _build_parser(version: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Kinetic SDK CLI Router: runs one of the kinetic_devops tools. Everything after the tool name is\n"
            "passed to that tool; router options (--reuse-last-session, --session-id, --tool-help) must come\n"
            "BEFORE the tool name."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops auth list\n"
            "  python -m kinetic_devops --reuse-last-session baq MyBAQ\n"
            "  python -m kinetic_devops <tool> -h      # options of one tool"
        ),
    )
    parser.add_argument("-v", "--version", action="version", version=f"Kinetic SDK v{version}")
    parser.add_argument(
        "--reuse-last-session",
        action="store_true",
        help="Reuse LAST_GLOBAL_SESSION for any subcommand that initializes a Kinetic session "
             "(exported to the tool as KINETIC_SESSION_ENV/USER/CO).",
    )
    parser.add_argument(
        "--session-id",
        metavar="ID",
        default="",
        help="Target a specific saved session ID suffix (shown as ID_xxxx). Cannot be combined with "
             "--reuse-last-session.",
    )
    parser.add_argument(
        "--tool-help",
        metavar="TOOL",
        choices=sorted(TOOLS.keys()),
        default="",
        help="Show help for a specific tool parser while keeping tool options defined at the source "
             "(same as '<tool> -h'). TOOL is one of: " + ", ".join(sorted(TOOLS.keys())) + ".",
    )
    subparsers = parser.add_subparsers(dest="tool", metavar="<tool>", help="Select the SDK tool to run")

    subparsers.add_parser("auth", help="Manage saved environments, API keys and cached tokens (keyring)")
    subparsers.add_parser("baq", help="Execute BAQ queries and save the rows to JSON")
    subparsers.add_parser("meta", help="MetaFX tools (fetch UI metadata, core layer import/delete operations)")
    subparsers.add_parser("export", help="Export customizations via ExportAllTheThings EFx or native endpoints")
    subparsers.add_parser("solutions", help="List, back up, recreate, build, install and remove Solution Workbench solutions")
    subparsers.add_parser("zdatatable", help="Detect/sync UD column drift from ZDataTable XML")
    subparsers.add_parser("find", help="Find sensitive data in the project (files, diffs, git history)")
    subparsers.add_parser("efx", help="Execute an Epicor Function (EFx) and print or save its output")
    subparsers.add_parser("report", help="Upload/deploy report packages and print reports to PDF")
    subparsers.add_parser("analysis", help="Run local AI and metadata-only analysis workflows")
    subparsers.add_parser("repomaker", help="RepoMaker modular tools (apply/reposmith/smoke)")
    subparsers.add_parser("scope", help="Access scope and API key migration operations")
    return parser


def _dispatch_tool(tool_name: str, tool_args: Sequence[str]) -> int:
    old_argv = sys.argv
    try:
        # Delegate directly to the submodule entrypoint to avoid runpy RuntimeWarning behavior.
        sys.argv = [f"{old_argv[0]} {tool_name}", *tool_args]
        result = TOOLS[tool_name]()
        return result if isinstance(result, int) else 0
    finally:
        sys.argv = old_argv


def main(argv: Sequence[str] | None = None) -> int:
    try:
        __version__ = importlib.metadata.version("kinetic-devops")
    except importlib.metadata.PackageNotFoundError:
        __version__ = "alpha-dev"

    args = list(argv) if argv is not None else sys.argv[1:]
    global_parser = _build_global_parser()
    global_args, remaining = global_parser.parse_known_args(args)

    if global_args.reuse_last_session and str(global_args.session_id or "").strip():
        print("Error: --reuse-last-session and --session-id are mutually exclusive.")
        return 2

    if str(global_args.tool_help or "").strip():
        return _dispatch_tool(global_args.tool_help, ["--help"])

    selected_session: dict[str, str] | None = None
    if global_args.reuse_last_session or str(global_args.session_id or "").strip():
        mgr = KineticConfigManager()
        try:
            if global_args.reuse_last_session:
                selected_session = _resolve_last_session(mgr)
            else:
                selected_session = _resolve_session_id(mgr, global_args.session_id)
        except ValueError as exc:
            print(f"Error: {exc}")
            return 2

    args = remaining
    parser = _build_parser(__version__)

    if not args:
        parser.print_help()
        return 1

    if args[0] in TOOLS:
        if selected_session:
            if selected_session.get("env"):
                os.environ["KINETIC_SESSION_ENV"] = selected_session["env"]
            if selected_session.get("user"):
                os.environ["KINETIC_SESSION_USER"] = selected_session["user"]
            if selected_session.get("co"):
                os.environ["KINETIC_SESSION_CO"] = selected_session["co"]
        return _dispatch_tool(args[0], args[1:])

    # This handles router-level arguments like --version/--help and unknown command errors.
    parser.parse_args(args)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())