---
module: vendors
title: Vendors
summary: First-class external translation and review vendors - a workflow can dispatch its packaged text to a human vendor and wait for a signed return, with MSA rate cards, an optional quote-and-approve spend gate, and a reject-and-rework loop, all without leaving the run.
nouns:
  Vendor: "An external translation or review partner, held as a first-class record with a name, a connection (endpoint URL plus a Fernet-encrypted signing secret), a transport protocol, and an MSA rate card. Link a vendor to the customers it may serve."
  Dispatch leg: "One packet of work sent out to a vendor and awaited - the unit the whole lifecycle tracks. It carries the segments, a cost estimate, a status, and (never echoed) a claim token."
  External step: "A workflow step of type external that hands its packaged text to a linked vendor and pauses the run until the vendor returns it. This is how work is dispatched - vendors are a workflow capability, not a side channel."
  Return contract: "The vendor's answer: the translated segments, admitted through one guarded seam whether they arrive by signed webhook callback, a poll, or a manual re-upload - as JSON or as XLIFF 1.2, which is accepted anywhere JSON is."
  Protocol: "How a leg is delivered - native (an automated signed wire) or manual (an operator carries the package out and the return back in). Snapshotted per leg, so flipping a vendor's protocol only affects future dispatches."
  Rate card: "A vendor's MSA pricing - a per-band cost per word (repetitions, context, exact, fuzzy tiers, new). It turns a leg's TM-leverage band breakdown into the Human-spend estimate."
  Quote gate: "An optional per-vendor requires_quote_approval posture: a dispatch parks as QUOTED awaiting a quote and an operator's approval before any work proceeds - the deliberate spend decision, separate from dispatching."
  Rework: "Rejecting a delivered leg and re-dispatching it to the same vendor with structured feedback. Rework rounds are capped (default 1), then the run escalates to a human review pause."
  Vendor Studio: "A companion app an operator can be handed off to (admin-only, browser-interactive) via a short-lived signed SSO token, when its URL and SSO secret are configured."
verbs:
  See your vendors: "Web: the Vendors page. CLI: `vendor list` (add --include-archived for retired ones), and `vendor show 5` for one vendor's connection state, rate card and linked customers (a secret is only ever reported as set, never shown)."
  Add a vendor: "Web: the Vendors page. CLI: `vendor add` - only a name is required; fill the endpoint, protocol, secret and rate card afterwards."
  Price the work: "Web: the rate-card panel. CLI: `vendor rate-card show 5` shows the per-band rates; set one band at a time so the others are untouched. The rate card is what turns a leg's band breakdown into the Human-spend estimate."
  Dispatch work: "Attach an external step to a workflow pointing at the vendor (the workflow builder's vendor picker, or `workflow add-step`). When the run reaches it, the packaged text is sent and the run waits for the signed return."
  Track what is out: "Web: the run view's leg panel, and the vendor detail page. CLI: `vendor dispatches` lists the open legs - their status, id counts, endpoint host (never the URL or claim token) and estimate-versus-actual cost."
  Gate the spend: "For a quote-gated vendor, record the quote and then approve or decline it. CLI: `vendor quote --dispatch-id 42 --amount 250` records the quote (it moves no money and does not advance the leg); `vendor approve-quote --dispatch-id 42` is the spend decision that lets work proceed; decline escalates to a human."
  Pull a return manually: "CLI: `vendor poll` asks vendors that expose a poll URL for ready returns; a return can also be re-uploaded by hand. All three routes - webhook, poll, manual - funnel through the one guarded return seam."
  Hand off as XLIFF: "The dispatch package also renders as XLIFF 1.2, the format every vendor-side CAT tool opens. A per-vendor `include_xliff` config flag (default off) embeds it in the outbound leg; you can always download it on demand - `GET /api/vendors/dispatches/{did}/xliff`, or CLI `vendor dispatch-show <did> --xliff-out FILE`. Inbound, an XLIFF 1.2 return is accepted anywhere a JSON return is."
needs:
  - "The vendors_enabled switch on. With it off every vendor surface is removed: the /vendors pages, the /api/vendors endpoints and the inbound vendor webhooks all return not-found (404), new dispatch egress is refused at the send seam naming the switch, the scheduler's vendor return-poll and dispatch-retry sweeps stop, and the CLI refuses. Existing vendors and dispatch history are preserved, just unreachable until it is switched back on."
  - "A vendor linked to the customer whose document is being dispatched - an external step can only select a vendor linked to that customer."
  - "A signing secret on the vendor for a native-protocol leg: the outbound dispatch and the signed return callback are HMAC-signed with it. A manual-protocol vendor needs no wire secret."
  - "A rate card if you want a Human-spend estimate; without one the band breakdown has no prices to apply."
  - "For the quote gate: only enable requires_quote_approval for a vendor whose system implements the quote callback (or whose operator records the quote by hand) - a vendor that ignores the quote request and simply delivers will have its delivery refused until a quote is approved."
limits:
  - "Dispatch is egress: when vendors are switched off, a run reaching an external step fails with a readable reason rather than sending - work already committed is never corrupted, but no new vendor work leaves the process."
  - "A leg's protocol is snapshotted at dispatch. Changing a vendor's protocol affects only future legs; an already-dispatched leg keeps the protocol it was sent under."
  - "A transient outbound failure parks the leg for a backoff retry (retry_wait); when the retry window is exhausted the leg becomes dead_letter and escalates to a human review pause, rather than silently dropping."
  - "Rework rounds are capped (default 1). Past the cap the run escalates to a human instead of looping on the same vendor forever."
  - "Deleting a vendor fails closed while a workflow step selects it or a dispatch references it - it never cascades."
  - "AI spend (model) and Human spend (vendor post-edit) are tracked and reported separately; the Human figure is an estimate until the vendor reconciles an actual cost on its return."
  - "Queries carried to a vendor share the WHOLE thread: a query you open on a segment that is out with a vendor is sent to them with the next package, and - because a thread is one conversation visible whole - your customer-bound client can see the vendor's replies in the same thread. You are told this at compose time; nobody has to discover it from the data."
  - "A vendor's comment crosses back only when the person writing it ticks share (default off, so internal vendor notes never ship), and the tick names both audiences: the buyer AND their end client will see a shared comment."
  - "On the XLIFF manual path, queries travel to the vendor and NOT back. A vendor working in a third-party CAT tool must return comments through Vendor Studio, the JSON return, or out of band - a CAT tool's arbitrary notes are never silently turned into customer-visible queries."
  - "Per segment a package carries at most 5 query threads x 10 newest posts; anything beyond is declared on the package (never a silent cut) and shown to the vendor as 'N more comments not shown'. A return admits at most 10 comments per segment; an over-cap or malformed comments block refuses the whole return, while an individual over-full thread or an invalid reply target is reported as a rejected comment and the rest of the return still admits."
connects:
  - module: workflows
    how: "A vendor is dispatched by an external workflow step, which pauses the run until the signed return arrives and then resumes it. Vendors are a workflow capability - the run is where dispatch, wait, quote-approval and rework all happen."
  - module: documents
    how: "A dispatch leg packages a document's segments (with additive per-segment provenance, match score and locked markers) and the return commits translated segments back through the same guarded write seam a normal translation uses."
  - module: tm
    how: "A leg's TM-leverage band breakdown - how much of the work is repetitions, exact, fuzzy or new - is what the vendor's rate card prices into the Human-spend estimate, so better TM coverage directly lowers the vendor quote."
  - module: engines
    how: "Vendor (Human) spend sits alongside model (AI) spend in the same ledger: AI plus Human is the run's total, so a vendor leg and an engine call are two priced lines on one run."
---

## What this is for

Some content should not be machine-translated and shipped - it should go to a human
translation or review vendor first. Vendors make that a first-class part of a
workflow: a run can **dispatch** its packaged text to a linked vendor, **wait** for
a signed return, and resume - with the vendor's price, the spend approval, and any
rework all handled inside the run rather than in email.

## The lifecycle

1. **Set up the vendor** - a connection, a signing secret for the native wire (or
   manual protocol for a carry-out/carry-in vendor), an MSA **rate card**, and the
   customers it may serve.
2. **Dispatch** - a workflow's **external step** sends the packaged segments to the
   vendor and pauses the run. Track it with `vendor dispatches`.
3. **Approve the spend** (optional) - a quote-gated vendor parks the leg as `QUOTED`;
   record the quote (`vendor quote --dispatch-id 42 --amount 250`) and then approve
   it (`vendor approve-quote --dispatch-id 42`) before work proceeds.
4. **Return** - the vendor's translated segments come back by signed webhook, by
   `vendor poll`, or by a manual re-upload, all through one guarded seam; the run
   resumes.
5. **Reject and rework** if needed - re-dispatch to the same vendor with structured
   feedback, capped so it escalates to a human rather than looping forever.

## Egress is refused when the feature is off

Switching vendors off **removes** the whole surface, but it never corrupts work in
flight. A run that reaches an external step **fails with a readable reason** instead
of sending; the scheduler stops its vendor network sweeps; committed content is
untouched. Turn the switch back on and every vendor and every parked leg is exactly
where it was.

## Queries: the round trip

Queries are a conversation about a segment, and they now cross the vendor wire in both
directions. When a run dispatches a segment that has open or answered buyer queries on it,
those threads ride out on the package (whole-thread, newest-capped) so the vendor sees the
question in context. A vendor working in **Vendor Studio** reads them above the local
comment list, marked as coming from the buyer, and can **reply and share** - a shared reply
comes back on the return and lands on the buyer's thread as a first-class post, flipping it
**answered**. A vendor's own internal notes stay internal unless the person writing them
ticks *share*, and that tick tells the whole truth: **the buyer AND their end client will
see it**, because a thread is one conversation visible whole.

Two honest edges. First, this whole-thread visibility is symmetric and disclosed on both
sides: the buyer is told at compose time that a query on a dispatched segment reaches the
vendor. Second, the XLIFF manual path is **one-way** - queries go out as per-unit notes and
nothing comes back through the parser, because a third-party CAT tool's arbitrary notes must
never silently become customer-visible queries. When the queries feature is switched off the
carry stops in both directions: the package omits the block entirely, and a vendor's return
still admits normally with its comments silently skipped (a vendor is never refused a
delivery because the buyer turned an internal feature off).

## Money: AI versus Human

Model spend and vendor post-edit spend are tracked as two lines - **AI + Human =
total** - with the Human figure labelled an estimate until the vendor reconciles an
actual on its return. The estimate comes from the leg's TM-leverage **band
breakdown** priced through the vendor's rate card, so the same TM leverage that
makes a machine run cheap makes the vendor quote smaller too.
