# MCP Workflow

The managed native MCP settings file is
`home/.cline/data/settings/cline_mcp_settings.json`.

Phase A writes:

```json
{
  "mcpServers": {}
}
```

This means no real MCP server is configured. It does not mean Cline MCP support
is disabled. Add a server only when there is a real source-owned adapter,
explicit transport configuration, isolated environment policy, and validation.
