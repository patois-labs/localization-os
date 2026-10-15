# 0080. MCP stdio file reads limited to allowed folders

Status: Accepted
Date: 2026-10-11

## Context

#755: over stdio the MCP server reads local files named by `file_path`, so an injected path could upload any file
on the machine.

## Decision

- File reads are limited to allowed folders: Documents, Downloads, OneDrive\Documents and the output folder by
  default, plus `LOC_STUDIO_INPUT_DIRS`. The Desktop is not a default.
- Refused: paths that resolve outside (junctions and symlinks followed), file symlinks, dotfiles and dot-folders,
  wrong extensions, relative paths, files over the size cap (100 MiB, images 8 MiB, checked before and after
  reading). The refusal names the allowed folders and the fix.
- Same batch (#754): `mcp_cms` speaks MCP 2026-07-28 with automatic fallback to the handshake protocol.
