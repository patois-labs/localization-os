# 0070. Undeclared text files read in the source language's code page, with a warning

Status: Accepted
Date: 2026-10-11

## Context

Subtitles, TXT, CSV and INI files carry no encoding declaration. Older files in Greek, Russian, Hebrew, Chinese and
other non-Western code pages were read as Windows-1252 (the #509 rule) and came out garbled. The maintainer approved this
(about 10:30 PM).

## Decision

- Decode order: BOM / valid UTF-8, UTF-16 BOM, declared charset, then the project's SOURCE language code page, then
  Windows-1252. Never a replacement decode; unreadable = refusal with "re-save as UTF-8". GBK falls back to GB18030.
- The code page used is stamped on the document (`text_code_page`); export, SVG render and repair re-read with the
  stamp. One code page per parse for containers. Unstamped documents keep the old rule exactly.
- Warn (never block) when the decoded letters look wrong, and for short CJK files that are valid UTF-8 by accident.
  A language's own accented letters never count as garbled (#760).

## Consequences

- Two known undecidable cases (one accented letter read as one valid hanzi in zh-CN/zh-TW) stay silent.
