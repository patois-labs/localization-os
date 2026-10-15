# 0088. Connected projects: a third project kind whose content comes from a connection

Status: Accepted
Date: 2026-10-13

## Context

Project automation (ADR 0082) let a project bind to a connection, but nothing in creation, the project page or the
list showed it. The maintainer asked for the automation and connector work to appear as its own kind of project.
Lokalise ("Marketing and support" type, one platform per project) and Smartling ("Connector" project type) do the
same; Phrase instead spawns a project per change (APC). The design is recorded here.

## Decision

- Three kinds, worded **Documents, Keys, Connected** everywhere a person reads them.
- Stored as `Project.intake` (`upload` / `connected`), separate from `project_type` (document / key_based shape).
  Intake can change only while the project is empty; a keys connector (github, gitlab, bitbucket) bound to an empty
  connected project sets `key_based`.
- **Create first, connect second:** creating a connected project opens a "Connect a source" setup menu (pick or
  connect a system, scope, languages, schedule, notifications, preview). A template never binds or schedules.
  Unbinding returns to the setup menu.
- Uploads and key imports are hidden and refused on connected projects.
- **The project's language list is the only source of truth** for a bound project: adding a language runs only the
  new pairs (manager or higher); removing one stops it (`targetlocale.stopped_at`), never deletes content or writes to
  the external system; agents (MCP) may only add; unbinding leaves stopped languages stopped. One row per normalized
  language; a connection keeps its vendor spelling. Editing a 1:1 bound connection's languages changes the project's;
  a connection feeding other projects through lanes refuses the edit (409).
- Connector first pulls create connected, bound, unscheduled projects; older connector projects convert only by a
  manager's "Make this a connected project". Nothing converts on upgrade.

## Consequences

Builds on ADR 0082 (bindings, lanes, claims) without changing connector state handling. Preview is a readiness check
until connectors gain a read-only listing. Airtable needs a matching column per language: accepted as a stated limit
until a real use case. Each project bound to a shared connection reads the source separately through
its own lane; no shared reader. Merged to main on 2026-10-13.

## Links

ADR 0082.
