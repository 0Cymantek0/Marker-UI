"""Marker headless CLI.

Run from the backend directory:
    python -m app.cli convert C:\path\to\document.pdf --output-dir output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.agent_api import (
    AgentConversionOptions,
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
from app.errors import ERROR_SCHEMA_VERSION, MarkerError, from_exception


def main(argv: list[str] | None = None) -> int:
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
            return _print_result(
                asyncio.run(list_jobs(page=args.page, page_size=args.page_size)),
                args.json,
            )
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
                asyncio.run(delete_job(args.job_id, delete_files=not args.keep_files)),
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
            from app.mcp_server import run

            run(transport=args.transport, host=args.host, port=args.port, auth_token=args.auth_token)
            return 0
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
    parser = argparse.ArgumentParser(prog="marker", description="Marker CLI and MCP server")
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
    sub = parser.add_subparsers(dest="command", required=True)

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

    jobs = sub.add_parser("jobs", help="List conversion job history")
    jobs.add_argument("--page", type=int, default=1)
    jobs.add_argument("--page-size", type=int, default=20)
    jobs.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    status = sub.add_parser("job-status", help="Show one conversion job")
    status.add_argument("job_id")
    status.add_argument("--include-result-text", action="store_true")
    status.add_argument("--max-chars", type=int, default=20_000)
    status.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    delete = sub.add_parser("delete-job", help="Cancel/delete one conversion job")
    delete.add_argument("job_id")
    delete.add_argument("--keep-files", action="store_true", help="Keep upload/output files")
    delete.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    settings = sub.add_parser("settings", help="List, get, set, or delete settings")
    settings_sub = settings.add_subparsers(dest="settings_command", required=True)
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

    test = sub.add_parser("self-test", help="Run CLI/MCP readiness checks")
    test.add_argument("--no-conversion", action="store_true", help="Skip real TSV conversion smoke test")
    test.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")

    mcp = sub.add_parser("mcp", help="Start MCP server")
    mcp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8000)
    mcp.add_argument(
        "--auth-token",
        help="Bearer token for streamable HTTP; defaults to MARKER_MCP_AUTH_TOKEN",
    )
    return parser


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


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-format", default="markdown", choices=["markdown", "json", "html", "chunks"])
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
    parser.add_argument("--audio-output-mode", choices=["transcript", "enhanced", "notes", "meeting_notes", "lecture_notes"])
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
    parser.add_argument("--ocr-engine", choices=["surya", "glm_ocr", "paddleocr_vl", "mistral_ocr"])
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
        "ocr_engine",
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
