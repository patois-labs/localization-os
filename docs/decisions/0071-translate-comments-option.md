# 0071. Translating comments is a per-project option, default off

Status: Accepted
Date: 2026-10-11

## Context

#690: Word translated comments, the other Office formats did not. The maintainer approved one per-project option for
every Office format, default off. Decision 8 in the same build: SVG title and desc (screen-reader text) are offered
for translation.

## Decision

- Setting `translate_comments:<project id>`; new projects (including inbox and connector projects) stamp "0" (off).
- Projects with no row are "pre-option": Word comments stay on, others off, so existing Word exports do not change.
- On: comments in Word, Excel (notes and threaded), PowerPoint (classic and modern), OpenDocument and Visio are
  translated; authors, initials, dates, ids and anchors are never units. Off: comment parts are byte-identical.
- Four surfaces: web toggle (manager+), `POST /api/projects/{pid}/translate-comments`, CLI `project set-comments`,
  MCP `configure_project action=comments`.
- SVG title/desc units are appended after `<text>` units, so existing segment ids stay put.
