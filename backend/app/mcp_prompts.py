"""MCP prompt templates for repeatable Marker workflows."""

from __future__ import annotations

from typing import Any


def register_mcp_prompts(mcp: Any) -> None:
    @mcp.prompt(
        name="convert_for_rag",
        title="Convert For RAG",
        description="Plan and convert a document for retrieval with honest format support.",
    )
    def convert_for_rag(
        input_path: str,
        output_dir: str,
        quality: str = "auto",
        allow_cloud_vlm: bool = False,
    ) -> str:
        return (
            f"Convert `{input_path}` for RAG into `{output_dir}` using quality `{quality}`. "
            f"Keep allow_cloud_vlm={allow_cloud_vlm}. Call capabilities, plan, convert or submit, "
            "request output_format=chunks only for Marker-backed sources that support it; otherwise read Markdown "
            "with bounded offset pages. Return manifest path, warnings, and retrieval notes."
        )

    @mcp.prompt(
        name="extract_tables_from_document",
        title="Extract Tables From Document",
        description="Convert a document and focus on table extraction quality.",
    )
    def extract_tables_from_document(input_path: str, output_dir: str = "") -> str:
        return (
            f"Plan `{input_path}` for table extraction, convert with a table-friendly profile, "
            f"write outputs under `{output_dir}` when provided, then inspect Markdown tables and manifest metadata."
        )

    @mcp.prompt(
        name="summarize_converted_document_with_citations",
        title="Summarize Converted Document With Citations",
        description="Read converted output pages and produce a cited summary.",
    )
    def summarize_converted_document_with_citations(output_path: str) -> str:
        return (
            f"Read `{output_path}` in bounded offset pages with marker_read_output_chunk. Summarize only supported "
            "claims, cite section/page cues found in the converted text, and include manifest/assets if relevant."
        )

    @mcp.prompt(
        name="convert_and_compare_two_documents",
        title="Convert And Compare Two Documents",
        description="Convert two documents and compare their content.",
    )
    def convert_and_compare_two_documents(left_path: str, right_path: str, output_dir: str = "") -> str:
        return (
            f"Plan and convert `{left_path}` and `{right_path}` into `{output_dir}`. Read both outputs in offset pages, "
            "compare structure, tables, figures, and material differences, then cite the relevant output pages."
        )

    @mcp.prompt(
        name="batch_convert_folder",
        title="Batch Convert Folder",
        description="Batch convert a folder of documents with safe defaults.",
    )
    def batch_convert_folder(folder_path: str, output_dir: str, continue_on_error: bool = True) -> str:
        return (
            f"List supported files under `{folder_path}`, convert them to `{output_dir}` one by one, "
            f"continue_on_error={continue_on_error}, and report successes, failures, manifests, and retry guidance."
        )

    @mcp.prompt(
        name="inspect_conversion_quality",
        title="Inspect Conversion Quality",
        description="Inspect output quality using manifest, assets, and source-aware checks.",
    )
    def inspect_conversion_quality(output_path: str) -> str:
        return (
            f"Inspect `{output_path}` by reading its manifest, assets, and representative output pages. "
            "Report missing text, table issues, image/figure handling, routing warnings, and suggested reconversion options."
        )

    @mcp.prompt(
        name="convert_audio_to_meeting_notes",
        title="Convert Audio To Meeting Notes",
        description="Convert audio into meeting notes with timestamps where available.",
    )
    def convert_audio_to_meeting_notes(input_path: str, output_dir: str = "") -> str:
        return (
            f"Convert audio `{input_path}` into meeting notes under `{output_dir}`. Use audio_output_mode=meeting_notes, "
            "request word timestamps when useful, then return action items, decisions, timestamps, and confidence warnings."
        )

    @mcp.prompt(
        name="extract_figures_and_diagrams",
        title="Extract Figures And Diagrams",
        description="Convert a document while preserving and describing figures/diagrams.",
    )
    def extract_figures_and_diagrams(
        input_path: str,
        output_dir: str = "",
        allow_cloud_vlm: bool = False,
    ) -> str:
        return (
            f"Plan and convert `{input_path}` into `{output_dir}` with image handling focused on figures and diagrams. "
            f"Keep allow_cloud_vlm={allow_cloud_vlm} unless the user approves cloud vision, then inspect assets and cite figure outputs."
        )
