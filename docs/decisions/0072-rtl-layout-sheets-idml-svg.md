# 0072. Right-to-left layout for spreadsheets, InDesign/InCopy and SVG

Status: Accepted
Date: 2026-10-11

## Context

Spreadsheets, IDML/ICML and SVG translated into Arabic, Hebrew or Persian kept a left-to-right layout; users had to
set direction by hand. The maintainer chose both.

## Decision

- RTL target only (every new path is gated on target direction = rtl; LTR outputs are byte-identical):
  - Excel and OpenDocument sheets: sheet views flipped right to left (`rightToLeft="1"` on worksheet parts).
  - InDesign/InCopy: translated paragraphs get `ParagraphDirection="RightToLeftDirection"` and the World-Ready
    composer `HL Composer Optyca`; an explicit LTR character direction in a rebuilt range becomes default.
  - SVG: text direction set and `text-anchor` mirrored (start/end; middle kept).
- Not seen: anchors set from a `<style>` sheet or CSS class (documented in help).

## Consequences

- The composer value comes from public non-Adobe sources (Adobe's IDML spec lists none); open one export in real
  InDesign before relying on it at a customer.
