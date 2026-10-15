# 0043. Keep the custom workflow engine and ingestion layer; strict dependency litmus

- Status: Accepted ("never re-litigate")
- Date: 2026-08-06

## Context

n8n and Okapi were evaluated as replacements for the workflow engine and file filters.

## Decision

- Keep the custom workflow engine (n8n ruled out: Sustainable Use License, about $50K a year OEM, Node
  runtime). Keep the homegrown ingestion/filter layer; Okapi only as an optional opt-in sidecar on real
  demand.
- Adopting a dependency: MIT/Apache/BSD only, library-shaped, and it must answer "the 80% question".
- Advanced Workflows (the "harness") is a wholly separate section (`app/services/advanced/`, flag
  `advanced_workflows_enabled`); the classic editor and engine stay untouched by it.

## Links

- docs/architecture/workflow-engine.md, advanced-workflows.md
