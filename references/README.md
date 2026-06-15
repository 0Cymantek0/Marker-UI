# references/markitdown — Developer Reference Clone

This directory contains a **read-only developer reference clone** of
[microsoft/markitdown](https://github.com/microsoft/markitdown), pinned to
tag **v0.1.6** (May 2026, commit `e144e0a2be95b34df17433bac904e635f2c5e551`).

## Purpose

- Study Markitdown's converter architecture, plugin registry, and content
  extraction patterns for porting relevant logic into this project's backend.
- This is **not** a runtime dependency. The clone is for offline browsing and
  reference only. No code from here is imported or executed at runtime.

## Source & License

| Field       | Value                                                       |
|-------------|-------------------------------------------------------------|
| Repository  | https://github.com/microsoft/markitdown                     |
| Tag         | v0.1.6                                                      |
| License     | **MIT License**                                             |
| Vicinage    | Compatible with Marker UI's GPL-3.0 license (MIT is GPL-compatible) |

## Attribution Template

When porting any file from this reference clone into `backend/app/`, add the
following header at the top of each ported file (fill in `<sha>`, `<file>`):

```python
# Portions adapted from microsoft/markitdown (MIT License).
# Source: https://github.com/microsoft/markitdown/blob/<sha>/packages/markitdown/src/markitdown/converters/<file>.py
# Used under the MIT license; compatible with this project's GPL-3.0 license.
```

## Gitignore

The path `references/markitdown/` is listed in `.gitignore` (line 80). This
entire directory will not appear in commits or affect CI pipelines.
