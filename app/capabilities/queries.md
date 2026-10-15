---
module: queries
title: Queries
summary: Threaded questions and comments on a project, document, segment or string key - one conversation per thread, visible whole to everyone who can see the project (including a customer-bound client), with an open -> answered -> closed lifecycle, an optional assignee, the vendor round trip, and email notifications. Web, /api, CLI and MCP in the same change. A query never blocks a workflow and never moves money.
nouns:
  Query thread: "One conversation, scoped to a segment, key, document or project. It carries a subject, a status (open / answered / closed), an author, an optional assignee, and an append-only list of posts. A thread is visible WHOLE to everyone who can see its project - there is no per-post or per-thread privacy split."
  Post: "One message in a thread. Human posts carry the author's email; a vendor post carries a free-text display label and never a user account. Posts are append-only - there is no edit and no per-post delete."
  Scope anchor: "What a thread is about - exactly one of a segment, a string key, a document or a project. The anchor resolves to the thread's project (the tenant anchor), which is the only thing access scoping speaks: you see a thread if and only if you can see its project."
  Status: "The lifecycle state. open (needs attention) -> answered (someone replied with an answer) -> closed (done). A plain reply never flips open to answered - only an explicit answer does (a machine cannot tell an answer from a 'me too'). A plain reply BY THE THREAD AUTHOR on an ANSWERED thread reopens it (open) - the author is re-asking, so their query needs attention again. A vendor reply IS the answer, so it does flip the thread to answered."
  Assignee: "The person a thread is on. Setting it emails them and puts the thread on their My work page. It must be an existing active user who is visible to the caller AND able to see the thread's own project - the field can never mail an arbitrary address, and an assignment can never become an obligation the assignee cannot open."
verbs:
  Open a query: "Web: the Queries page, or an affordance where the question is (the editor row, a document/project/key link). /api: POST /api/queries with one scope anchor + subject + body. CLI: `query open --segment N --body B [--subject S] [--assignee E]` (or --key/--document/--project). MCP: open_query. ANYONE who can view the project may open one - including a customer-bound client (that is the point)."
  Read queries: "Web: the /queries list (filter by status, project, document, assignee=me, mine) and the thread view. /api: GET /api/queries and GET /api/queries/{id}. CLI: `query list` / `query show <id>`. MCP: list_queries / get_query."
  Reply / answer: "Web: the reply box on a thread (tick 'mark answered' to answer). /api: POST /api/queries/{id}/posts with answer=true|false. CLI: `query reply <id> --body B` (plain) / `query answer <id> --body B` (marks answered). MCP: reply_to_query."
  Close / reopen: "Web: the Close / Reopen buttons. /api: POST /api/queries/{id}/status. CLI: `query close <id>` / `query close <id> --reopen`. MCP: set_query_status. Closing needs the author, the assignee or a reviewer+; reopening needs the author or a reviewer+."
  Assign: "Web: the Set assignee box. /api: POST /api/queries/{id}/assign (empty clears). CLI: `query assign <id> --assignee E`. MCP: assign_query. Needs the author or a reviewer+."
needs:
  - "The queries_enabled switch on (default ON). With it off the feature is REMOVED, not hidden: /queries and every POST 404 (themed, never 403 - non-enumerable), /api/queries* 404s (JSON), the nav item and the in-context affordances disappear, this card 404s on /help and /api/capabilities, the `query` CLI group refuses (`queries are disabled (setting queries_enabled=0).`), and the six MCP tools relay the 404. Existing threads are kept, dormant and invisible; re-enabling restores every thread, post, status and assignment byte-identically. No data is touched either way. (Cascade deletes and GDPR export/erase are NOT feature-gated - referential integrity and a legal erasure obligation are never behind a feature flag.)"
  - "Only the ability to VIEW the project - opening, reading, replying and answering need no role rung, so a viewer or a customer-bound client can raise and answer a query (RV-1). Closing and reopening need the author, the assignee or a reviewer+; assigning needs the author or a reviewer+. The in-process CLI is the trusted operator (no RBAC) and writes as --actor or 'cli'."
  - "An existing active user in scope to name as an assignee - the assign field validates the address against a real, active user who is both visible to you AND able to see the thread's own project, and refuses anything else with one message (no existence oracle across tenants). Requiring the assignee to see the project is what keeps an assignment from becoming a silent obligation the assignee can never open; requiring the address to resolve is what stops the field being turned into a mail relay. A concrete consequence: a customer-bound account can only ever be an assignee on ITS OWN customer's projects - it can never be assigned on a customer-less (global) project, or on another customer's, because it could not open the thread. The same validation guards the vendor-comment path: a workflow step naming an address that cannot see the project leaves the vendor-opened thread unassigned rather than mailing a dead obligation."
  - "SMTP configured for notifications to actually send. Without it every notification is a silent no-op (see limits). Nothing else is required - a query is a conversation, not a job."
limits:
  - "Caps are REFUSALS that name the limit, never silent truncation: a subject is limited to 200 characters; a post body to 4000; a thread to 200 posts; a segment to 20 open threads; a list page returns at most 50 (with the true total). Every one is a refusal on every surface."
  - "A thread is one conversation, visible WHOLE to everyone who can see the project - there is no internal/private post. If a segment is out with a vendor, a query on it is carried to the vendor with the next package, and a customer-bound client can see the vendor's replies in the same thread. The compose form says so at the time you write; the vendor's reply-share checkbox names both audiences (you AND your end client)."
  - "Notifications are EMAIL ONLY. There is no in-app notification centre, no digest, no Slack or webhook target, and no per-user preferences. On a new query the assignee is emailed; on a reply the author and assignee (minus whoever posted) are; on close the author (unless they closed it). An UNASSIGNED query notifies nobody, by design. If SMTP is not configured, no notification is sent and NOTHING is logged (`mailer.configured()` returns False and the send is a silent no-op) - the only in-app signal that a query is waiting for you is the My work page and the query lists. The user-typed subject never enters the email Subject: header (a fixed template, flattened)."
  - "Posts are append-only. There is no edit, no per-post delete, and no thread delete on any surface - 'withdraw' is close. A thread and its posts are removed only when their parent project / document / segment / key is deleted (the cascade), or by a GDPR erasure."
  - "A query never gates a workflow, never re-translates, and never moves money. Opening or reading one is always safe. Assignment is per-thread, not a workflow step - it creates an obligation and a My work entry, nothing more."
  - "Vendor carry is one-way over the XLIFF manual path: queries travel OUT to the vendor as per-unit notes, but a third-party CAT tool's arbitrary notes are NEVER admitted back as customer-visible posts. Comments come back only through Vendor Studio or the JSON return."
  - "The in-context affordances lead to the scoped query list. The editor row carries a LIVE count badge of the unresolved (open or answered) threads on that segment; the document, project and key panels are plain links to the scoped list. Every affordance disappears with the queries kill switch."
connects:
  - module: documents
    how: "A query most often anchors to a segment or document you are reviewing - the editor row and the document page link straight to the thread where the question lives."
  - module: vendors
    how: "Open queries on a dispatched segment ride out on the vendor package, and a vendor's shared comments come back as first-class posts in the same thread - the buyer<->vendor round trip."
  - module: workflows
    how: "A vendor-originated query is assigned to the paused review step's assignee (or the run's owner) so it lands on a human's My work page instead of dying in a table."
---

## What this is for

A query is how a question about specific content gets asked, answered and closed **where the
content is** - on a segment, a string key, a document, or a whole project - instead of in an
email thread nobody else can see. Each thread is one conversation with an
`open -> answered -> closed` lifecycle and an optional assignee, and it is visible **whole** to
everyone who can see the project: your team, and - deliberately - a customer-bound client, who
is exactly the person RV-1 wants able to raise a question in the first place.

## Who can do what

Opening, reading, replying and answering need only the ability to **view** the project - no
role rung - so a viewer or a client can raise and answer a query. Closing and reopening need the
thread's author, its assignee, or a **reviewer or higher**; assigning needs the author or a
reviewer+. Deleting is nobody's verb on any surface: a thread is append-only, and `close` is the
exit.

## The vendor round trip

When a segment is out with a vendor, an open or answered query on that segment is carried to the
vendor with the next package, and the vendor's **shared** comments come back as first-class posts
in the same thread. Because the thread is visible whole, both disclosures are shown at compose
time: the buyer is told the vendor (and their own client) will see what they write, and the
vendor's share checkbox names **both** audiences. Over the XLIFF manual path queries travel out
and **not** back - a third-party CAT tool's arbitrary notes must never silently become
customer-visible queries.

## Notifications, honestly

Query notifications are **email only**. There is no in-app notification centre, no digest, no
Slack or webhook target, and no per-user notification preferences. If SMTP is not configured, no
notification is sent and **nothing is logged** - the only in-app signal that a query is waiting
for you is the **My work** page and the query lists. An unassigned query notifies nobody, by
design.
