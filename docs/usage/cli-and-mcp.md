# CLI and MCP Quickstart

Marker can run without the browser through the CLI or an MCP server. Both paths
share the same backend conversion service, output writer, safety policy, and
agent JSON contracts.

Use this page as the short entry point. Detailed references live in:

- [CLI Guide](cli.md)
- [MCP Guide](mcp.md)
- [Agent JSON Schemas](../reference/json-schemas.md)
- [Agent Error Codes](../reference/errors.md)
- [Output Manifest Reference](../reference/output-manifest.md)
- [Enterprise Security](../enterprise/security.md)

## CLI Smoke Test

Run from `backend` in a source checkout:

```powershell
python -m app.cli self-test --json
```

Common local conversion flow:

```powershell
python -m app.cli capabilities --json
python -m app.cli plan "C:\path\to\document.pdf" --conversion-profile auto --json
python -m app.cli convert "C:\path\to\document.pdf" --output-dir "C:\path\to\out" --json
python -m app.cli read-output "C:\path\to\out\document.md" --offset 20000 --limit 20000 --json
```

Keep cloud image understanding off unless the user explicitly allows it:

```powershell
python -m app.cli convert "C:\path\to\scan.pdf" --image-handling-mode both --allow-cloud-vlm --json
```

## MCP Smoke Test

Start local stdio MCP from `backend`:

```powershell
python -m app.cli mcp
```

Start loopback Streamable HTTP:

```powershell
python -m app.cli mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

Binding Streamable HTTP to a non-loopback host requires a bearer token:

```powershell
$env:MARKER_MCP_AUTH_TOKEN="change-this-token"
python -m app.cli mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

Agents should first read `marker://capabilities`, plan unknown inputs, convert
or submit jobs, then page large outputs with `marker_read_output_chunk`.
