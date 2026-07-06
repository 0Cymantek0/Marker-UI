"""Marker headless CLI.

Run from the backend directory:
    python -m app.cli convert C:\path\to\document.pdf --output-dir output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.agent_api import (
    AgentConversionOptions,
    cancel_job,
    capabilities,
    convert_document,
    delete_job,
    delete_setting,
    parse_extra_options,
    parse_extra_options_json,
    get_job_status,
    get_setting,
    list_jobs,
    list_settings,
    plan_conversion,
    read_output,
    set_setting,
    self_test,
    submit_conversion_job,
)
from app.agent_contract import AUDIO_OUTPUT_MODES, export_json_schemas
from app.conversion.formats import OUTPUT_FORMATS
from app.errors import ERROR_SCHEMA_VERSION, MarkerError, from_exception
from app.eval.runner import run_eval


BATCH_PARTIAL_FAILURE_EXIT = 10
_CURRENT_ARGV: list[str] = []


class MarkerArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if _argv_wants_json(_CURRENT_ARGV):
            payload = {
                "schema_version": ERROR_SCHEMA_VERSION,
                "ok": False,
                "error": {
                    "code": "USAGE_ERROR",
                    "message": message,
                    "hint": "Run marker --help or the subcommand --help for usage.",
                    "details": {},
                },
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(2)
        super().error(message)


def _argv_wants_json(argv: list[str]) -> bool:
    return "--json" in argv


def main(argv: list[str] | None = None) -> int:
    global _CURRENT_ARGV
    _CURRENT_ARGV = list(argv if argv is not None else sys.argv[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            return _print_result(capabilities(), args.json)
        if args.command == "plan":
            opts = _options_from_args(args)
            result = asyncio.run(
                plan_conversion(
                    local_file_path=args.input,
                    filename=args.filename,
                    size=args.size,
                    options=opts,
                )
            )
            return _print_result(result, args.json)
        if args.command == "convert":
            opts = _options_from_args(args)
            result = asyncio.run(
                convert_document(
                    local_file_path=args.input,
                    source_url=args.source_url,
                    output_dir=args.output_dir,
                    output_path=args.output_path,
                    max_chars=args.max_chars,
                    options=opts,
                )
            )
            return _print_result(result, args.json)
        if args.command == "submit-job":
            opts = _options_from_args(args)
            result = asyncio.run(
                submit_conversion_job(
                    local_file_path=args.input,
                    source_url=args.source_url,
                    output_dir=args.output_dir,
                    options=opts,
                )
            )
            return _print_result(result, args.json)
        if args.command == "read-output":
            return _print_result(
                read_output(args.path, offset=args.offset, limit=args.limit),
                args.json,
            )
        if args.command == "jobs":
            return _handle_jobs(args)
        if args.command == "output":
            return _handle_output(args)
        if args.command == "batch":
            return asyncio.run(_handle_batch(args))
        if args.command == "doctor":
            return _handle_doctor(args)
        if args.command == "hybrid-ocr":
            return _handle_hybrid_ocr(args)
        if args.command == "schema":
            return _handle_schema(args)
        if args.command == "eval":
            return _handle_eval(args)
        if args.command == "config":
            return _handle_config(args)
        if args.command == "server":
            return _handle_server(args)
        if args.command == "job-status":
            return _print_result(
                asyncio.run(
                    get_job_status(
                        args.job_id,
                        include_result_text=args.include_result_text,
                        max_chars=args.max_chars,
                    )
                ),
                args.json,
            )
        if args.command == "delete-job":
            return _print_result(
                asyncio.run(delete_job(args.job_id, delete_files=not args.keep_files, force=args.force)),
                args.json,
            )
        if args.command == "settings":
            return _handle_settings(args)
        if args.command == "self-test":
            return _print_result(
                asyncio.run(self_test(include_conversion=not args.no_conversion)),
                args.json,
            )
        if args.command == "mcp":
            return _handle_mcp(args)
        parser.print_help()
        return 2
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - CLI must present clean errors
        return _handle_cli_error(exc, args)


def _handle_cli_error(exc: BaseException, args: argparse.Namespace | None) -> int:
    """Convert any exception to a stable CLI failure (exit code + output).

    JSON mode emits one ``marker.error.v1`` object on stderr. Non-JSON mode
    prints ``Error: <message>`` on stderr. Stack traces only when ``--debug``.
    """

    as_json = bool(getattr(args, "json", False))
    debug = bool(getattr(args, "cli_debug", False))
    err = from_exception(exc)
    payload = err.to_payload()
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), file=sys.stderr)
    else:
        hint = f" Hint: {err.hint}" if err.hint else ""
        print(f"Error: {err.message}{hint}", file=sys.stderr)
    if debug:
        import traceback

        traceback.print_exc(file=sys.stderr)
    return err.exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = MarkerArgumentParser(prog="marker", description="Marker CLI and MCP server")
    parser.add_argument(
        "--version",
        action="version",
        version=_version_text(),
        help="Print version and exit",
    )
    parser.add_argument(
        "--debug",
        dest="cli_debug",
        action="store_true",
        help="Print stack traces on error (global)",
    )
    parser.add_argument(
        "--quiet",
        dest="cli_quiet",
        action="store_true",
        help="Suppress non-error diagnostics (global)",
    )
    parser.add_argument(
        "--verbose",
        dest="cli_verbose",
        action="store_true",
        help="Emit extra diagnostic messages (global)",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="Never prompt for interactive confirmation (global)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Assume yes for confirmations where supported (global)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print intended action without writing where supported (global)",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=MarkerArgumentParser)

    caps = sub.add_parser("capabilities", help="List supported formats, engines, and MCP tools")
    caps.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    plan = sub.add_parser("plan", help="Plan conversion without running it")
    plan.add_argument("input", nargs="?", help="Local file path")
    plan.add_argument("--filename", help="Filename for metadata-only planning")
    plan.add_argument("--size", type=int, default=0, help="Input size for metadata-only planning")
    _add_common_options(plan)
    plan.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    conv = sub.add_parser("convert", help="Convert a local file or public URL")
    conv.add_argument("input", nargs="?", help="Local file path")
    conv.add_argument("--source-url", help="Public http(s) source URL")
    conv.add_argument("--output-dir", help="Directory for converted output")
    conv.add_argument("--output-path", help="Exact output text file path")
    conv.add_argument("--max-chars", type=int, default=20_000, help="Preview chars printed in response")
    _add_common_options(conv)
    conv.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    submit = sub.add_parser("submit-job", help="Submit an async conversion job")
    submit.add_argument("input", nargs="?", help="Local file path")
    submit.add_argument("--source-url", help="Public http(s) source URL")
    submit.add_argument("--output-dir", help="Directory for converted output")
    _add_common_options(submit)
    submit.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    read = sub.add_parser("read-output", help="Read a slice of a converted text output")
    read.add_argument("path", help="Output file path")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=20_000)
    read.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    jobs = sub.add_parser("jobs", help="List/manage conversion jobs")
    jobs.add_argument("--page", type=int, default=1)
    jobs.add_argument("--page-size", type=int, default=20)
    jobs.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=False, parser_class=MarkerArgumentParser)
    jobs_list = jobs_sub.add_parser("list", help="List conversion job history")
    jobs_list.add_argument("--page", type=int, default=1)
    jobs_list.add_argument("--page-size", type=int, default=20)
    jobs_list.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    jobs_status = jobs_sub.add_parser("status", help="Show one conversion job")
    jobs_status.add_argument("job_id")
    jobs_status.add_argument("--include-result-text", action="store_true")
    jobs_status.add_argument("--max-chars", type=int, default=20_000)
    jobs_status.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    jobs_watch = jobs_sub.add_parser("watch", help="Poll one job until terminal status")
    jobs_watch.add_argument("job_id")
    jobs_watch.add_argument("--include-result-text", action="store_true")
    jobs_watch.add_argument("--max-chars", type=int, default=20_000)
    jobs_watch.add_argument("--interval", type=float, default=1.0)
    jobs_watch.add_argument("--max-polls", type=int, default=0, help="Stop after N polls; 0 means until terminal")
    jobs_watch.add_argument("--progress", choices=["text", "ndjson"], default="text")
    jobs_watch.add_argument("--json", action="store_true", help="Print final JSON")
    jobs_delete = jobs_sub.add_parser("delete", help="Delete one conversion job")
    jobs_delete.add_argument("job_id")
    jobs_delete.add_argument("--keep-files", action="store_true", help="Keep upload/output files")
    jobs_delete.add_argument("--force", action="store_true", help="Cancel and delete a pending/running job")
    jobs_delete.add_argument("--yes", action="store_true", help="Confirm deletion")
    jobs_delete.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    jobs_cancel = jobs_sub.add_parser("cancel", help="Cancel one job best-effort without deleting its record")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    status = sub.add_parser("job-status", help="Show one conversion job")
    status.add_argument("job_id")
    status.add_argument("--include-result-text", action="store_true")
    status.add_argument("--max-chars", type=int, default=20_000)
    status.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    delete = sub.add_parser("delete-job", help="Delete one terminal conversion job")
    delete.add_argument("job_id")
    delete.add_argument("--keep-files", action="store_true", help="Keep upload/output files")
    delete.add_argument("--force", action="store_true", help="Cancel and delete a pending/running job")
    delete.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    output = sub.add_parser("output", help="Read converted outputs")
    output_sub = output.add_subparsers(dest="output_command", required=True, parser_class=MarkerArgumentParser)
    output_read = output_sub.add_parser("read", help="Read a slice of a converted text output")
    output_read.add_argument("path", help="Output file path")
    output_read.add_argument("--offset", type=int, default=0)
    output_read.add_argument("--limit", type=int, default=20_000)
    output_read.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    batch = sub.add_parser("batch", help="Convert multiple inputs sequentially")
    batch.add_argument("inputs", nargs="*", help="Local file paths to convert")
    batch.add_argument("--request-json", help="Batch request JSON file with items")
    batch.add_argument("--output-dir", help="Directory for converted outputs")
    batch.add_argument("--results-path", help="Optional JSON results file")
    batch.add_argument("--failed-path", help="Optional JSON failures file")
    batch.add_argument("--continue-on-error", action=argparse.BooleanOptionalAction, default=True)
    batch.add_argument("--resume", action="store_true", help="Skip items whose output path already exists")
    batch.add_argument("--dry-run", action="store_true", help="Validate request without converting")
    _add_common_options(batch)
    batch.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    doctor = sub.add_parser("doctor", help="Run environment and conversion readiness checks")
    doctor.add_argument("--no-conversion", action="store_true", help="Skip real TSV conversion smoke test")
    doctor.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    hybrid = sub.add_parser("hybrid-ocr", help="Inspect or set up Hybrid OCR specialist models")
    hybrid_sub = hybrid.add_subparsers(dest="hybrid_ocr_command", required=True, parser_class=MarkerArgumentParser)
    hybrid_status = hybrid_sub.add_parser("status", help="Show Hybrid OCR model/runtime readiness")
    hybrid_status.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    hybrid_setup = hybrid_sub.add_parser("setup", help="Download Hybrid OCR model snapshots")
    hybrid_setup.add_argument("--engine", choices=["glm_ocr", "paddleocr_vl", "all"], default="all")
    hybrid_setup.add_argument("--force", action="store_true", help="Re-download even if snapshot directory exists")
    hybrid_setup.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    schema = sub.add_parser("schema", help="Inspect/export stable JSON schemas")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True, parser_class=MarkerArgumentParser)
    schema_export = schema_sub.add_parser("export", help="Export agent contract JSON schemas")
    schema_export.add_argument("--output", help="Write schemas to this JSON file")
    schema_export.add_argument("--dry-run", action="store_true", help="Print without writing output file")
    schema_export.add_argument("--json", action="store_true", default=True, help="Print JSON")

    eval_cmd = sub.add_parser("eval", help="Run deterministic evaluation manifests")
    eval_sub = eval_cmd.add_subparsers(dest="eval_command", required=True, parser_class=MarkerArgumentParser)
    eval_run = eval_sub.add_parser("run", help="Run an eval manifest and write JSON/Markdown reports")
    eval_run.add_argument("--manifest", required=True, help="Path to eval manifest JSON")
    eval_run.add_argument("--output-dir", required=True, help="Directory for reports")
    eval_run.add_argument("--report-name", default="eval_report", help="Report filename stem")
    eval_run.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    settings = sub.add_parser("settings", help="List, get, set, or delete settings")
    settings_sub = settings.add_subparsers(dest="settings_command", required=True, parser_class=MarkerArgumentParser)
    settings_list = settings_sub.add_parser("list", help="List masked settings")
    settings_list.add_argument("--category")
    settings_list.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    settings_get = settings_sub.add_parser("get", help="Get one masked setting")
    settings_get.add_argument("key")
    settings_get.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    settings_set = settings_sub.add_parser("set", help="Set one setting")
    settings_set.add_argument("key")
    settings_set.add_argument("value")
    settings_set.add_argument("--category", default="general")
    settings_set.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    settings_delete = settings_sub.add_parser("delete", help="Delete one setting")
    settings_delete.add_argument("key")
    settings_delete.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    config = sub.add_parser("config", help="Alias for settings list/get/set/delete")
    config_sub = config.add_subparsers(dest="settings_command", required=True, parser_class=MarkerArgumentParser)
    config_list = config_sub.add_parser("list", help="List masked settings")
    config_list.add_argument("--category")
    config_list.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    config_get = config_sub.add_parser("get", help="Get one masked setting")
    config_get.add_argument("key")
    config_get.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    config_set = config_sub.add_parser("set", help="Set one setting")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.add_argument("--category", default="general")
    config_set.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    config_delete = config_sub.add_parser("delete", help="Delete one setting")
    config_delete.add_argument("key")
    config_delete.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    test = sub.add_parser("self-test", help="Run CLI/MCP readiness checks")
    test.add_argument("--no-conversion", action="store_true", help="Skip real TSV conversion smoke test")
    test.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    server = sub.add_parser("server", help="Local server client-mode helpers")
    server_sub = server.add_subparsers(dest="server_command", required=True, parser_class=MarkerArgumentParser)
    server_status = server_sub.add_parser("status", help="Report client-mode server status")
    server_status.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    server_start = server_sub.add_parser("start", help="Print local server start command")
    server_start.add_argument("--host", default="127.0.0.1")
    server_start.add_argument("--port", type=int, default=8000)
    server_start.add_argument("--dry-run", action="store_true", help="Do not launch a server")
    server_start.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    server_stop = server_sub.add_parser("stop", help="Report stop guidance for externally managed server")
    server_stop.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    mcp = sub.add_parser("mcp", help="Start or configure MCP server")
    mcp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8000)
    mcp.add_argument(
        "--auth-token",
        help="Bearer token for streamable HTTP; defaults to MARKER_MCP_AUTH_TOKEN",
    )
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=False, parser_class=MarkerArgumentParser)
    mcp_start = mcp_sub.add_parser("start", help="Start MCP server")
    mcp_start.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mcp_start.add_argument("--host", default="127.0.0.1")
    mcp_start.add_argument("--port", type=int, default=8000)
    mcp_start.add_argument("--auth-token", help="Bearer token for streamable HTTP")
    mcp_init = mcp_sub.add_parser("init-config", help="Generate an MCP client configuration snippet")
    mcp_init.add_argument("--client", choices=["codex", "claude", "gemini", "opencode", "antigravity"], required=True)
    mcp_init.add_argument("--output", help="Write config JSON to file instead of stdout")
    mcp_init.add_argument("--dry-run", action="store_true", help="Print without writing output file")
    mcp_init.add_argument("--json", action="store_true", default=True, help="Print JSON")
    mcp_inspect = mcp_sub.add_parser("inspect", help="Inspect MCP tool names without starting transport")
    mcp_inspect.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    mcp_self = mcp_sub.add_parser("self-test", help="Run MCP readiness checks")
    mcp_self.add_argument("--no-conversion", action="store_true", help="Skip real TSV conversion smoke test")
    mcp_self.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    return parser


def _version_text() -> str:
    version = os.getenv("MARKER_VERSION", "0.1.0")
    commit = os.getenv("MARKER_COMMIT_SHA", "").strip()
    suffix = f" ({commit})" if commit else ""
    return f"marker {version}{suffix}"


def _handle_settings(args: argparse.Namespace) -> int:
    if args.settings_command == "list":
        return _print_result(
            asyncio.run(list_settings(category=args.category)),
            args.json,
        )
    if args.settings_command == "get":
        return _print_result(asyncio.run(get_setting(args.key)), args.json)
    if args.settings_command == "set":
        return _print_result(
            asyncio.run(set_setting(args.key, args.value, category=args.category)),
            args.json,
        )
    if args.settings_command == "delete":
        return _print_result(asyncio.run(delete_setting(args.key)), args.json)
    return 2


def _handle_config(args: argparse.Namespace) -> int:
    return _handle_settings(args)


def _handle_eval(args: argparse.Namespace) -> int:
    if args.eval_command == "run":
        result = run_eval(args.manifest, args.output_dir, report_name=args.report_name)
        return _print_result(result, args.json)
    return 2


def _handle_jobs(args: argparse.Namespace) -> int:
    command = getattr(args, "jobs_command", None) or "list"
    if command == "list":
        return _print_result(
            asyncio.run(list_jobs(page=args.page, page_size=args.page_size)),
            args.json,
        )
    if command == "status":
        return _print_result(
            asyncio.run(
                get_job_status(
                    args.job_id,
                    include_result_text=args.include_result_text,
                    max_chars=args.max_chars,
                )
            ),
            args.json,
        )
    if command == "watch":
        return asyncio.run(_watch_job(args))
    if command == "delete":
        return _print_result(
            asyncio.run(delete_job(args.job_id, delete_files=not args.keep_files, force=args.force)),
            args.json,
        )
    if command == "cancel":
        result = asyncio.run(cancel_job(args.job_id))
        return _print_result(result, args.json)
    return 2


async def _watch_job(args: argparse.Namespace) -> int:
    polls = 0
    last: dict[str, Any] | None = None
    terminal = {"completed", "failed", "cancelled"}
    while True:
        polls += 1
        last = await get_job_status(
            args.job_id,
            include_result_text=args.include_result_text,
            max_chars=args.max_chars,
        )
        if args.progress == "ndjson":
            print(json.dumps(last, ensure_ascii=False, default=str))
        elif not args.json:
            print(f"{last['job_id']} {last['status']} {last.get('progress', 0)}%")
        if last.get("status") in terminal:
            break
        if args.max_polls and polls >= args.max_polls:
            break
        await asyncio.sleep(max(0.1, args.interval))
    return _print_result(last or {}, args.json) if args.json else 0


def _handle_output(args: argparse.Namespace) -> int:
    if args.output_command == "read":
        return _print_result(
            read_output(args.path, offset=args.offset, limit=args.limit),
            args.json,
        )
    return 2


def _handle_doctor(args: argparse.Namespace) -> int:
    result = asyncio.run(self_test(include_conversion=not args.no_conversion))
    from app.hybrid_ocr.capability import detect_capabilities
    from app.hybrid_ocr.setup import hybrid_setup_status

    caps = detect_capabilities()
    result["doctor"] = {
        "schema_version": "marker.doctor.v1",
        "checks": {
            "capabilities": bool(result.get("capabilities_ok")),
            "conversion": result.get("conversion_ok"),
            "hybrid_ocr": "glm_ocr" in {engine.value for engine in caps.available}
            or "paddleocr_vl" in {engine.value for engine in caps.available},
        },
        "hybrid_ocr": {
            **hybrid_setup_status(),
            "engines_available": sorted(engine.value for engine in caps.available),
            "warnings": caps.warnings,
        },
    }
    exit_code = 0 if result.get("capabilities_ok") and result.get("conversion_ok") is not False else 1
    _print_result(result, args.json)
    return exit_code


def _handle_hybrid_ocr(args: argparse.Namespace) -> int:
    from app.hybrid_ocr.capability import detect_capabilities
    from app.hybrid_ocr.setup import download_model_snapshot, hybrid_setup_status

    if args.hybrid_ocr_command == "status":
        caps = detect_capabilities()
        return _print_result(
            {
                "schema_version": "marker.hybrid_ocr_status.v1",
                **hybrid_setup_status(),
                "engines_available": sorted(engine.value for engine in caps.available),
                "warnings": caps.warnings,
                "runtime_env": {
                    "glm_endpoint": bool(os.environ.get("MARKER_GLM_OCR_ENDPOINT")),
                    "glm_command": bool(os.environ.get("MARKER_GLM_OCR_COMMAND")),
                    "paddle_endpoint": bool(os.environ.get("MARKER_PADDLE_OCR_VL_ENDPOINT")),
                    "paddle_command": bool(os.environ.get("MARKER_PADDLE_OCR_VL_COMMAND")),
                },
            },
            args.json,
        )
    if args.hybrid_ocr_command == "setup":
        engines = ["glm_ocr", "paddleocr_vl"] if args.engine == "all" else [args.engine]
        results = [download_model_snapshot(engine, force=args.force) for engine in engines]
        return _print_result(
            {
                "schema_version": "marker.hybrid_ocr_setup.v1",
                "results": results,
                "status": hybrid_setup_status(),
            },
            args.json,
        )
    return 2


def _handle_schema(args: argparse.Namespace) -> int:
    if args.schema_command != "export":
        return 2
    schemas = export_json_schemas()
    if args.output and not args.dry_run:
        Path(args.output).expanduser().write_text(
            json.dumps(schemas, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        schemas = {**schemas, "written_to": str(Path(args.output).expanduser().resolve())}
    return _print_result(schemas, True)


async def _handle_batch(args: argparse.Namespace) -> int:
    items = _batch_items_from_args(args)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if args.dry_run:
        payload = _batch_payload(items=items, results=[], failures=[], skipped=len(items), exit_code=0)
        _write_batch_outputs(args, payload, failures)
        _print_result(payload, args.json)
        return 0

    for index, item in enumerate(items):
        if _batch_should_skip(args, item):
            results.append(
                {
                    "index": index,
                    "ok": True,
                    "skipped": True,
                    "reason": "output_path exists and --resume is enabled",
                    "output_path": item.get("output_path"),
                }
            )
            continue
        try:
            result = await convert_document(
                local_file_path=item.get("local_file_path"),
                source_url=item.get("source_url"),
                output_dir=item.get("output_dir") or args.output_dir,
                output_path=item.get("output_path"),
                options=_batch_options(args, item),
            )
            results.append({"index": index, "ok": True, "result": result})
        except BaseException as exc:  # noqa: BLE001 - batch records typed per-item failures
            err = from_exception(exc).to_payload()
            failure = {"index": index, "ok": False, "error": err["error"]}
            failures.append(failure)
            results.append(failure)
            if not args.continue_on_error:
                break

    exit_code = BATCH_PARTIAL_FAILURE_EXIT if failures else 0
    payload = _batch_payload(
        items=items,
        results=results,
        failures=failures,
        skipped=max(0, len(items) - len(results)),
        exit_code=exit_code,
    )
    _write_batch_outputs(args, payload, failures)
    _print_result(payload, args.json)
    return exit_code


def _batch_items_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.request_json:
        raw = json.loads(Path(args.request_json).expanduser().read_text(encoding="utf-8"))
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("items", [])
            if "continue_on_error" in raw:
                args.continue_on_error = bool(raw["continue_on_error"])
            if "resume" in raw:
                args.resume = bool(raw["resume"])
        else:
            items = []
        return [dict(item) for item in items if isinstance(item, dict)]
    return [{"local_file_path": value} for value in args.inputs]


def _batch_options(args: argparse.Namespace, item: dict[str, Any]) -> AgentConversionOptions:
    item_options = item.get("options")
    if isinstance(item_options, dict):
        return AgentConversionOptions(**item_options)
    return _options_from_args(args)


def _batch_payload(
    *,
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    skipped: int,
    exit_code: int,
) -> dict[str, Any]:
    return {
        "schema_version": "marker.batch_result.v1",
        "ok": not failures,
        "total": len(items),
        "succeeded": len([item for item in results if item.get("ok") and not item.get("skipped")]),
        "failed": len(failures),
        "skipped": skipped + len([item for item in results if item.get("skipped")]),
        "exit_code": exit_code,
        "results": results,
        "failures": failures,
    }


def _batch_should_skip(args: argparse.Namespace, item: dict[str, Any]) -> bool:
    if not args.resume:
        return False
    output_path = item.get("output_path")
    return bool(output_path and Path(str(output_path)).expanduser().exists())


def _write_batch_outputs(
    args: argparse.Namespace,
    payload: dict[str, Any],
    failures: list[dict[str, Any]],
) -> None:
    if args.results_path:
        Path(args.results_path).expanduser().write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        payload["results_path"] = str(Path(args.results_path).expanduser().resolve())
    if args.failed_path:
        Path(args.failed_path).expanduser().write_text(
            json.dumps(failures, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        payload["failed_path"] = str(Path(args.failed_path).expanduser().resolve())


def _handle_server(args: argparse.Namespace) -> int:
    command = args.server_command
    if command == "status":
        return _print_result(
            {
                "schema_version": "marker.server_status.v1",
                "status": "external",
                "managed_by_cli": False,
                "message": "No managed local server process is tracked by this CLI yet.",
            },
            args.json,
        )
    if command == "start":
        result = {
            "schema_version": "marker.server_start.v1",
            "dry_run": True,
            "command": [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                args.host,
                "--port",
                str(args.port),
            ],
        }
        return _print_result(result, args.json)
    if command == "stop":
        return _print_result(
            {
                "schema_version": "marker.server_stop.v1",
                "status": "not_managed",
                "message": "Stop the external server process that launched Marker UI.",
            },
            args.json,
        )
    return 2


def _handle_mcp(args: argparse.Namespace) -> int:
    command = getattr(args, "mcp_command", None) or "start"
    if command == "start":
        from app.mcp_server import run

        run(transport=args.transport, host=args.host, port=args.port, auth_token=args.auth_token)
        return 0
    if command == "init-config":
        config = _mcp_client_config(args.client)
        if args.output and not args.dry_run:
            Path(args.output).expanduser().write_text(
                json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            config = {**config, "written_to": str(Path(args.output).expanduser().resolve())}
        return _print_result(config, True)
    if command == "inspect":
        result = capabilities()
        result["mcp"] = {"transport": "stdio", "command": ["python", "-m", "app.cli", "mcp", "start"]}
        return _print_result(result, args.json)
    if command == "self-test":
        from app.mcp_server import marker_self_test

        return _print_result(
            asyncio.run(marker_self_test(include_conversion=not args.no_conversion)),
            args.json,
        )
    return 2


def _mcp_client_config(client: str) -> dict[str, Any]:
    base = {
        "command": "python",
        "args": ["-m", "app.cli", "mcp", "start"],
        "env": {"MARKER_PRELOAD_MODELS": "false"},
    }
    if client in {"codex", "claude"}:
        return {"mcpServers": {"marker": base}}
    if client == "gemini":
        return {"mcpServers": {"marker": base}}
    if client == "opencode":
        return {"mcp": {"marker": base}}
    if client == "antigravity":
        return {"servers": {"marker": base}}
    return {"marker": base}


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-format", default="markdown", choices=list(OUTPUT_FORMATS))
    parser.add_argument("--converter-cls")
    parser.add_argument("--engine-override")
    parser.add_argument("--conversion-profile", choices=["auto", "fast", "high_accuracy"])
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--llm-provider")
    parser.add_argument("--llm-model")
    parser.add_argument("--image-handling-mode", default="extraction", choices=["extraction", "understanding", "both"])
    parser.add_argument("--allow-cloud-vlm", action="store_true")
    parser.add_argument("--force-ocr", action="store_true")
    parser.add_argument("--paginate-output", action="store_true")
    parser.add_argument("--disable-image-extraction", action="store_true")
    parser.add_argument("--page-range")
    parser.add_argument("--lang")
    parser.add_argument("--audio-output-mode", choices=list(AUDIO_OUTPUT_MODES))
    parser.add_argument("--audio-model")
    parser.add_argument("--audio-vocabulary")
    parser.add_argument("--audio-context")
    parser.add_argument("--audio-low-confidence-threshold", type=float)
    parser.add_argument("--audio-word-timestamps", action="store_true")
    parser.add_argument("--disable-multiprocessing", action="store_true")
    parser.add_argument("--strip-existing-ocr", action="store_true")
    parser.add_argument("--redo-inline-math", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--text-data-max-rows", type=int, help="Max CSV/TSV rows to inline in Markdown")
    parser.add_argument("--archive-max-files", type=int, help="Max archive entries to inspect")
    parser.add_argument("--archive-inline-bytes", type=int, help="Max bytes to inline per archive text child")
    parser.add_argument("--archive-max-child-bytes", type=int, help="Max bytes per converted archive child")
    parser.add_argument("--archive-max-depth", type=int, help="Max recursive archive conversion depth")
    parser.add_argument("--archive-max-converted-children", type=int, help="Max archive children to convert")
    parser.add_argument(
        "--archive-recursive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable recursive archive child conversion",
    )
    parser.add_argument(
        "--router-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable image-understanding router",
    )
    parser.add_argument("--smart-router-level", choices=["disabled", "smart", "beeg_brain"])
    parser.add_argument(
        "--dedup-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Deduplicate repeated images before understanding",
    )
    parser.add_argument(
        "--downscale-vlm-crops",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Downscale image crops before VLM calls",
    )
    parser.add_argument(
        "--batch-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Batch image-understanding route/extract calls",
    )
    parser.add_argument("--ocr-engine", default="surya", choices=["surya", "hybrid_ocr"])
    parser.add_argument("--hybrid-ocr-profile", default="balanced", choices=["balanced", "max_accuracy", "low_vram"])
    parser.add_argument("--hybrid-ocr-require-specialists", action="store_true")
    parser.add_argument("--decorative-max-text-density", type=float)
    parser.add_argument("--ocr-min-text-density", type=float)
    parser.add_argument("--ocr-min-lines", type=int)
    parser.add_argument("--dedup-max-distance", type=int)
    parser.add_argument("--vlm-crop-max-px", type=int)
    parser.add_argument("--vlm-batch-size", type=int)
    parser.add_argument("--max-batch-retries", type=int)
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        help="Advanced GUI-compatible option as key=value. Repeat as needed.",
    )
    parser.add_argument(
        "--options-json",
        default="",
        help="Advanced GUI-compatible options as one JSON object.",
    )


def _options_from_args(args: argparse.Namespace) -> AgentConversionOptions:
    return AgentConversionOptions(
        output_format=args.output_format,
        converter_cls=args.converter_cls,
        engine_override=args.engine_override,
        conversion_profile=args.conversion_profile,
        use_llm=args.use_llm,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        image_handling_mode=args.image_handling_mode,
        allow_cloud_vlm=args.allow_cloud_vlm,
        force_ocr=args.force_ocr,
        paginate_output=args.paginate_output,
        disable_image_extraction=args.disable_image_extraction,
        page_range=args.page_range,
        lang=args.lang,
        audio_output_mode=args.audio_output_mode,
        audio_model=args.audio_model,
        audio_vocabulary=args.audio_vocabulary,
        audio_context=args.audio_context,
        audio_low_confidence_threshold=args.audio_low_confidence_threshold,
        audio_word_timestamps=args.audio_word_timestamps,
        disable_multiprocessing=args.disable_multiprocessing,
        strip_existing_ocr=args.strip_existing_ocr,
        redo_inline_math=args.redo_inline_math,
        ocr_engine=args.ocr_engine,
        hybrid_ocr_profile=args.hybrid_ocr_profile,
        hybrid_ocr_require_specialists=args.hybrid_ocr_require_specialists,
        debug=args.debug,
        extra_options={
            **_direct_extra_options(args),
            **parse_extra_options(args.option),
            **parse_extra_options_json(args.options_json),
        },
    )


def _direct_extra_options(args: argparse.Namespace) -> dict[str, Any]:
    option_names = (
        "text_data_max_rows",
        "archive_max_files",
        "archive_inline_bytes",
        "archive_max_child_bytes",
        "archive_max_depth",
        "archive_max_converted_children",
        "archive_recursive",
        "router_enabled",
        "smart_router_level",
        "dedup_enabled",
        "downscale_vlm_crops",
        "batch_enabled",
        "decorative_max_text_density",
        "ocr_min_text_density",
        "ocr_min_lines",
        "dedup_max_distance",
        "vlm_crop_max_px",
        "vlm_batch_size",
        "max_batch_retries",
    )
    return {
        name: getattr(args, name)
        for name in option_names
        if getattr(args, name, None) is not None
    }


def _print_result(result: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    print(_to_markdown(result))
    return 0


def _to_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    if "plan" in result:
        plan = result["plan"]
        lines.append(f"# Plan: {plan['label']}")
        lines.append(f"- Engine: `{plan['engine']}`")
        lines.append(f"- Confidence: {plan['confidence']}")
        lines.extend(f"- Reason: {reason}" for reason in plan.get("reasons", []))
        if plan.get("warnings"):
            lines.extend(f"- Warning: {warning}" for warning in plan["warnings"])
        return "\n".join(lines)
    if "output" in result:
        lines.append("# Conversion Complete")
        lines.append(f"- Output: `{result['output']['text_path']}`")
        lines.append(f"- Characters: {result['text_chars']}")
        lines.append(f"- Truncated: {result['truncated']}")
        if result.get("text_preview"):
            lines.extend(["", "## Preview", "", result["text_preview"]])
        return "\n".join(lines)
    if "text" in result and "path" in result:
        return result["text"]
    if "converters" in result:
        lines.append("# Marker Capabilities")
        lines.append(f"- Service: `{result['service']}`")
        lines.append(f"- Tools: {', '.join(result['tools'])}")
        lines.append(f"- Extensions: {', '.join(result['allowed_extensions'])}")
        lines.append("")
        lines.append("## Engines")
        for item in result["converters"]:
            lines.append(f"- `{item['engine']}`: {', '.join(item['extensions']) or 'metadata only'}")
        return "\n".join(lines)
    if "jobs" in result:
        lines.append("# Conversion Jobs")
        lines.append(f"- Total: {result['total']}")
        for job in result["jobs"]:
            lines.append(f"- `{job['job_id']}` {job['status']} {job.get('filename') or ''}")
        return "\n".join(lines)
    if "settings" in result:
        lines.append("# Settings")
        for category, items in result["settings"].items():
            lines.append(f"## {category}")
            for item in items:
                lines.append(f"- `{item['key']}`: {item['value']}")
        return "\n".join(lines)
    if {"key", "value", "category"}.issubset(result):
        return f"# Setting\n- Key: `{result['key']}`\n- Category: `{result['category']}`\n- Value: {result['value']}"
    if "job_id" in result and "status" in result:
        lines.append(f"# Job {result['job_id']}")
        lines.append(f"- Status: {result['status']}")
        lines.append(f"- Progress: {result.get('progress', 0)}")
        if result.get("filename"):
            lines.append(f"- File: {result['filename']}")
        if result.get("result_path"):
            lines.append(f"- Result: `{result['result_path']}`")
        return "\n".join(lines)
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
