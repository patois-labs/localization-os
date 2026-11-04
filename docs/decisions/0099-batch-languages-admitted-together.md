# 0099. A document's languages are admitted together

Status: Accepted (amends the batch-turn part of 0092 and 0098)
Date: 2026-10-24

## Context

The real-LLM flood after ADR 0098 fixed parking and starvation (longest wait between steps 0.1 s), but
only 54% of documents had every language at review within 15 s in the default single-process layout (max spread
32 s). The admission scheduler took ONE slot per admission. With 8 runs and 1 interactive reserve the queue holds 7,
so once it was full a document's second and third languages each waited for a different run to end and started
10-30 s apart; the batch's turn (0092) only kept the ORDER together, not the time. Layouts whose queue share is a
multiple of 3 (web + 2 workers of 4 runs: 97.4%; `LOC_TMS_MAX_CONCURRENT_RUNS=7`: 94.9%) kept whole documents in step
by accident.

## Decision (start a document's languages together)

- **A batch's turn is one admission.** When the fair pick (0098 order, unchanged) lands on a batch fan-out,
  `_gang_for` sizes the gang: what its turn has left, what its width allows now (width minus active members), and
  never more than the queue's share of the cap. A batch wider than that starts in whole chunks.
- **Bounded wait for slots.** `_gather_slots` holds the slots that free until there is one per language, at most
  `LOC_TMS_BATCH_GANG_WAIT_S` (default 30 s, about two real steps, the same bound as the quantum) and never longer
  than the pass's own slot wait. Held slots cannot be handed to other queued work, which is what split documents.
  Past the wait the scheduler starts the languages it has slots for and the rest follow (their batch keeps its
  turn), so a batch never waits on slots that will not free, and nothing deadlocks: the daemon is the only taker
  that waits while holding slots (the keys fan-out's inline pass and a synchronous CLI batch take free slots
  without waiting).
- **Interactive launches still get through.** The interactive reserve slot is outside the queue's share, so a
  person's launch finds it free while a batch gathers slots, and the `QUEUE_YIELD_S` window still applies.
- **One transaction across worker processes** (S3b/S3c, ADR 0095/0097). `_admit_gang` claims the members RUNNING
  in ONE transaction that first writes the batch row (a row lock on Postgres, the write lock on SQLite), then counts
  the batch's active members and claims only up to its width. A second worker's gang claim of the same batch waits
  milliseconds and finds the rows taken, and two workers can no longer each claim a width. A member another worker
  already took one at a time (`_admit`) is skipped and the rest start together; a queued resume row (already in
  flight) goes through the one-run path.
- **Parked languages park and continue as one.** The first language of a batch to reach a step boundary decides
  for its siblings at that place (`run_segments._should_park`, keyed by step index and boundary count, entries
  expire after 5 minutes): deciding each on its own, one language found no other customer waiting and went on while
  its sibling, a moment later, parked, and they reached review a step apart. A run's own next step still comes
  first: a shared "park" never parks a run before its review or the end. The pick does not offer a batch's parked
  language while a sibling running in the same process may still park at that step
  (`_siblings_still_stepping`, `run_segments.heading_to_park`), for at most `LOC_TMS_BATCH_GANG_WAIT_S`; the
  scheduler neither blocks nor holds a slot meanwhile, and the sibling's park (or its decision not to) wakes it.
  Then the parked languages continue in one admission.
- Everything that is not a batch fan-out (keys translate, single runs, plain fan-outs) is a gang of one: no change.

## Consequences

- While a batch gathers slots the slots it holds are idle, at most `LOC_TMS_BATCH_GANG_WAIT_S` per batch. Started
  together, a document's languages tend to end together, so slots free in groups and the wait is short in steady
  state; on a queue share that is not a multiple of the batch width the leftover slot goes to the next document's
  gather. Leftover slots after a gang (more free than the gang needs) go to the next pick as before.
- The 0098 wait cap on a parked run can be delayed by one gather and by waiting for its siblings (each bounded by
  `LOC_TMS_BATCH_GANG_WAIT_S`).
- Review round 1 found the shared decision
  could park a run before its own review (fixed: the run's own next step comes first), that the parked gather held
  idle slots while siblings finished (fixed: the pick waits instead, holding nothing) and that two workers could
  each claim a batch's width (fixed: the batch row is written first). Round 2: while a
  customer's parked languages wait for a sibling, none of that customer's new work starts (finish started work
  first); the sibling wait never exceeds the 0098 cap; a run that decided to park but could not wakes its
  siblings; the spend and fail-fast stop writes the batch row before the run rows (one lock order with the claim).
- Rotation state, parked runs and the gather stay per worker process; the one-transaction claim is what holds a
  document together across processes.
- Proof: `verify_fair_dispatch` G1-G8 (G5 end to end, two customers, slow-mock steps longer than the quantum on a
  queue of 5: start spread 0.01 s and review spread 0.12 s, against 2.25 s and 2.03 s before). A real-LLM
  flood rerun confirms it separately.
