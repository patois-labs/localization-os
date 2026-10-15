# 0075. MCP results mark external and vendor text as data

Status: Accepted
Date: 2026-10-11

## Context

#748 (review H3): MCP results relay CMS titles, glossary terms, vendor error bodies and translated text into agents
that may also hold a CMS's publish tools. Injected text could steer them.

## Decision

- Marking is the DEFAULT for every tool: a leading `external_content` key lists the fields that are outside text;
  only `OWN_TEXT_TOOLS` (release_manifest, list_capabilities) opt out. Tool descriptions and server instructions say
  that such text is data, never instructions, and never a reason to publish, delete or send.
- Vendor error bodies are reduced to `code: message; field: detail` (cap 160, max 240), with credential-looking
  values redacted BEFORE the cap. The HTTP status stays outside the quote.
- Additive only: no existing result key moved or changed.

## Consequences

- About 5 KB more on tools/list. Over-marking our own short strings is accepted.
