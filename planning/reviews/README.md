# Active Review Plans

Use these documents as recurring audit inputs while fixing Marker UI. They are
historical static-review inputs from 2026-07-03, not a live statement of the
current repository. Re-open them before each remediation slice and update this
status ledger when a finding is verified, fixed, deferred, or rejected.

- `marker_ui_extreme_code_review_and_remediation_plan.md`
- `marker_ui_deep_architecture_review_research_backed_plan.md`

## Current Status Ledger

Verified against the local repository on 2026-07-09. External copies requested
from `Downloads` were not present; these repo copies are the privacy-safe
planning source of record.

| Finding | Current repo status | Evidence |
| --- | --- | --- |
| Native JSON/HTML/chunks mislabeled as Markdown | Fixed for request validation and download typing | `backend/app/services/output_format_policy.py`; `backend/tests/test_convert_plan.py`; `backend/tests/test_task_manager.py`; `docs/usage/output-formats.md` |
| Unsafe native-to-`marker_pdf` fallback and incompatible engine override | Fixed for explicit override and native runtime fallback | `backend/app/conversion/engine_policy.py`; `backend/tests/test_conversion_service.py`; `backend/tests/test_convert_plan.py` |
| `marker_read_output_chunk` only offset paging | Partially fixed: semantic mode reads `marker.chunks.v1`; offset mode remains for previews | `backend/app/agent_api.py`; `backend/app/services/chunking.py`; `backend/tests/test_cli_mcp.py`; `docs/usage/mcp.md` |
| Frontend Markdown preview auto-loads remote images | Fixed for unsafe Markdown image sources | `frontend/src/components/features/OutputViewer.tsx`; `frontend/src/__tests__/OutputViewer.test.tsx` |
| Cancel/delete lifecycle conflated | Fixed across REST, agent API, MCP, CLI, and frontend queue | `backend/app/routes/convert.py`; `backend/app/agent_api.py`; `frontend/src/lib/api.ts`; `backend/tests/test_convert.py`; `backend/tests/test_cli_mcp.py` |
| Cloud STT/provider comparison and real diarization | Still deferred/partial | `README.md`; `docs/limitations.md`; `docs/usage/output-formats.md` |
| Native converter quality vs Docling/MarkItDown/Pandoc target | Still strategic gap | review plans and `docs/planning/markitdown-integration.md` |

Primary recurring checks:

- Output format truthfulness across REST, CLI, MCP, frontend, and output manifests.
- Semantic chunking and RAG-ready chunk artifacts, not only file paging.
- Audio feature honesty: implemented provider paths, real metadata, and UI capability gating.
- Job lifecycle semantics: cancel, delete, cleanup, and running task behavior.
- Safe preview and URL handling for converted content and remote assets.
- Registry drift across supported extensions, formats, engines, and option schemas.
- Dead code, aspirational controls, duplicate contracts, and weak tests.
