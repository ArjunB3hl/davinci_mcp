# DaVinci Resolve MCP Server

An MCP server that controls DaVinci Resolve through its Scripting API. This
copy lives at `/Users/arjunbehl/davinci_mcp` and is registered in Windsurf.

## Requirements

- **DaVinci Resolve 20.x.** Install a 20.x build, not the latest. This server
  is live-tested on Studio 20.3.2. Resolve 21.1 moved Python scripting to the
  Studio edition only, which broke the free-edition path. Older builds are on
  Blackmagic's support page: <https://www.blackmagicdesign.com/support/family/davinci-resolve-and-fusion>
- **Studio edition preferred.** The free edition works on 20.x only through
  the in-app bridge (Workspace > Scripts > resolve_bridge), which the server
  uses automatically when external scripting is unavailable.
- **In Resolve:** Preferences > General > External scripting using = Local.
- **Python 3.10 or newer.** The source install runs on 3.13.
- **ffmpeg on PATH** for the media analysis tools (optional).

## Install

Create the venv in place. Never copy a venv from another location; venvs have
absolute paths baked in.

```sh
python3 -m venv venv
venv/bin/pip install "mcp[cli]>=1.30,<2"
venv/bin/pip install -r requirements.txt
venv/bin/python --version   # must be 3.10 or newer
```

Do not upgrade mcp past 1.x. `src/server.py` imports `mcp.server.fastmcp`,
which the 2.0 release removed.

Smoke test without Resolve running. The expected output is one JSON line
containing `"serverInfo"`:

```sh
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}\n' | venv/bin/python src/server.py 2>/dev/null | head -c 400
```

If it prints nothing, see Troubleshooting.

## Connect to Windsurf

Config file location:

| OS            | Path                                       |
| ------------- | ------------------------------------------ |
| macOS / Linux | `~/.codeium/windsurf/mcp_config.json`      |
| Windows       | `%APPDATA%\windsurf\mcp_settings.json`     |

Merge this into the file (create it if missing, keep any existing
`mcpServers` entries). Absolute paths only: Windsurf spawns the process with
no shell and no PATH.

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "/Users/arjunbehl/davinci_mcp/venv/bin/python",
      "args": ["/Users/arjunbehl/davinci_mcp/src/server.py"]
    }
  }
}
```

Leave out the `env` block. `src/utils/platform.py` auto-detects the Resolve
install paths. Only add `RESOLVE_SCRIPT_API` and `RESOLVE_SCRIPT_LIB` env
entries if the server later reports that it cannot find Resolve.

Verify:

1. Restart Windsurf.
2. Open the MCP panel and confirm `davinci-resolve` shows as connected and
   lists tools (38 compound tools, starting with `setup` and
   `resolve_control`).
3. Call the `setup` tool with action `schema`. It needs no running Resolve
   and proves the round trip.
4. With Resolve open, call `resolve_control` with action `status` to prove
   the Resolve connection.

## Troubleshooting

**The smoke test prints nothing.** Run it again without `2>/dev/null` to see
the traceback. Common causes: the `logs/` directory is missing (create it
next to `src/`), the wrong mcp version is installed (must be 1.x, check with
`venv/bin/pip show mcp`), or the venv Python is older than 3.10.

**Windsurf shows the server as disconnected.** Check that both paths in the
config are absolute and point at this directory's `venv/bin/python` and
`src/server.py`. Check that `logs/` exists. Run the smoke test above from a
terminal; if it passes there, the problem is in the config file.

**Tools list but Resolve calls fail.** Resolve is not running, or
Preferences > General > External scripting using is not set to Local. Start
Resolve, open a project, fix the preference, and retry. The error's own
`remediation` field names the fix that applies. On the free edition, make
sure the in-app bridge is available (Workspace > Scripts > resolve_bridge).

## Logs

The server never prints to stdout except MCP protocol JSON. Diagnostics go to
`logs/server.log`.
