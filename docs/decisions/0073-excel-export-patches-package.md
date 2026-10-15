# 0073. Excel export patches the original package

Status: Accepted
Date: 2026-10-11

## Context

#685: the Excel export rebuilt the workbook through openpyxl and dropped text boxes, shapes and EMF/WMF pictures
(15 soak failures). Word and PowerPoint already write into the original package. The maintainer chose to rewrite now.

## Decision

- Translated cells (and text boxes, walk 2) are written into the original .xlsx/.xlsm package. Untouched members
  keep bytes, order, compression and timestamps; only changed parts are re-serialized. Strict files stay Strict;
  `vbaProject.bin` is kept.
- The walk version is stamped (`xlsx_walk`): old projects export with walk 1 (cells only); an unknown stamp asks for
  a re-import. Text-box units are numbered after the last cell, so cell segment ids do not move.
- Shared strings: rewritten in place only when every reference gets the same translation, else a new string is
  appended. A formula overwritten by text drops the calc chain; shared-formula dependents get their own formula.
- All parts parse through the hardened XML parser; zip-bomb guard first.

## Consequences

- Chart text and VML comments are not walked yet. Mixed-format rich cells still lose run formatting (needs its own
  walk version).
