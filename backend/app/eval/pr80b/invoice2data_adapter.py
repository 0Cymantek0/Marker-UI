"""invoice2data specialist adapter (deterministic, offline).

invoice2data is the canonical open-source template-based invoice
extractor. The committed template encodes the corpus's canonical
vendor layout exactly once - the way an integrator configures it in
practice - so layout variants, broken rows, and unusual encodings
fail the way they really fail in production.

Adapter policy (declared): when the library returns a list of matches
for one field, the first match is taken, mirroring what integrators
do with its array outputs; no conflict semantics are invented for it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.eval.pr80b.scoring import (
    ABSENT,
    EMITTED,
    EmittedField,
    EmittedRow,
    SystemDocOutput,
)

SYSTEM_ID = "invoice2data"

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_SCALARS = ("invoice_number", "invoice_date", "currency", "po_number", "total_due")
#: invoice2data canonical field name -> benchmark field name.
_CANONICAL_MAP = {
    "invoice_number": "invoice_number",
    "date": "invoice_date",
    "currency": "currency",
    "po_number": "po_number",
    "amount": "total_due",
}
_ROW_MEMBERS = ("description", "quantity", "unit_price", "amount")


def _first(value: Any) -> Any:
    """Collapse the library's list outputs to their first match."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


class Invoice2DataAdapter:
    """Runs invoice2data over corpus documents in plain-text mode."""

    def __init__(self, template_dir: Path | None = None) -> None:
        from invoice2data.extract.loader import read_templates

        directory = Path(template_dir) if template_dir is not None else TEMPLATE_DIR
        self.templates = read_templates(str(directory))
        if not self.templates:
            raise ValueError(f"no invoice2data templates found in {directory}")
        self.template_names = [t.get("template_name", "?") for t in self.templates]

    def extract(self, doc_id: str, doc_text: str, workdir: Path) -> SystemDocOutput:
        from invoice2data import extract_data
        from invoice2data.input import text as text_reader

        run_dir = Path(workdir) / "invoice2data"
        run_dir.mkdir(parents=True, exist_ok=True)
        doc_path = run_dir / f"{doc_id}.txt"
        doc_path.write_text(doc_text, encoding="utf-8", newline="\n")
        started = time.perf_counter()
        try:
            result = extract_data(
                str(doc_path),
                templates=self.templates,
                input_module=text_reader,
                raise_on_error=False,
            )
        except Exception as exc:  # honest adapter failure capture
            return self._error_output(doc_id, f"{type(exc).__name__}: {exc}", started)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if not isinstance(result, dict) or not result:
            # The library returns {} when a template matched but its
            # required fields did not; integrators treat that as failure.
            return self._error_output(
                doc_id,
                "no template matched or required fields missing",
                started,
            )
        fields: dict[str, EmittedField] = {}
        for canonical, benchmark in _CANONICAL_MAP.items():
            value = _first(result.get(canonical))
            if value is None:
                fields[benchmark] = EmittedField(status=ABSENT)
            else:
                fields[benchmark] = EmittedField(status=EMITTED, value=str(value))
        rows: list[EmittedRow] = []
        items = result.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                sku = item.get("sku")
                rows.append(
                    EmittedRow(
                        sku=str(sku).strip() if sku is not None else None,
                        fields={
                            name: (
                                EmittedField(status=EMITTED, value=str(item[name]).strip())
                                if item.get(name) is not None
                                else EmittedField(status=ABSENT)
                            )
                            for name in _ROW_MEMBERS
                        },
                    )
                )
        return SystemDocOutput(
            system_id=SYSTEM_ID,
            doc_id=doc_id,
            fields=fields,
            rows=tuple(rows),
            run_status="template_matched",
            invariant_findings=None,
            raw={
                "timings_ms": {"extract_ms": elapsed_ms},
                "raw_result_keys": sorted(result.keys()),
                "template_names": list(self.template_names),
            },
        )

    def _error_output(self, doc_id: str, error: str, started: float) -> SystemDocOutput:
        return SystemDocOutput(
            system_id=SYSTEM_ID,
            doc_id=doc_id,
            fields={},
            rows=(),
            error=error,
            raw={
                "timings_ms": {"extract_ms": round((time.perf_counter() - started) * 1000, 2)}
            },
        )
