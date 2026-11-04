# 0098. Step parking finishes started work first; languages park together; no starvation

Status: Accepted (amends the hand-off and fair-dispatch parts of 0092)
Date: 2026-10-23

## Context

A real-LLM flood on the 0092 build made every language equal but every language slow: time to review p50 about
590 s, against 75 / 163 / 253 s on the build before it. With the 10 s quantum of 0092 and real LLM steps of about 15 s under load, 93-96% of runs parked
at every step boundary and waited 151-181 s each time; the fair rotation over every waiting fan-out turned the queue
into processor sharing (all runs progress together, all finish late). The park right before Review bought nothing
(Review only pauses). Siblings split at parks (28% of documents more than 15 s apart). With a 60 s quantum parked
runs starved for 15-20 minutes: the "next key greater than the last served" cursor never wrapped while new fan-outs
kept arriving with greater keys, and the keys were compared as text ("batch:9" after "batch:89").

## Decision (finish started work first, no park before review, park languages together, oldest-first with a wait
cap, 30-60 s quantum)

- **Finish started work first, per customer.** A run parks only when ANOTHER customer's work waits
  (`RunSegment._should_park`, `ADMISSION.has_waiting(other_customer_than=...)`), and the pick serves the chosen
  customer's parked runs before any of its new work. Parking is now what lets another customer in, not what
  interleaves one customer's documents.
- **No park before a step that does no work for the slot**: a human review, an external (vendor) step, or the end
  of the workflow (`_next_step_is_pause_only`).
- **Languages together.** A run of a batch parks at a boundary when a sibling of the same batch is parked at the
  same or an earlier step (`_sibling_parked_behind`), and the pick continues a fan-out's parked members one after
  the other, the fan-out whose member parked earliest first.
- **Least-recently-served turns with a wait cap** (`_lru_pick`): customers and fan-outs take turns by when they were
  last served; a fan-out seen for the first time joins behind everything already served; ties go to the oldest run
  id (numeric). A parked run that has waited `LOC_TMS_STEP_PARK_MAX_WAIT_S` (default 60 s) goes first, whatever the
  rotation says, on at most every other pick. Under sustained overload a customer that has only new work can still
  wait until the started backlog drains (bounded by the runs in flight and their remaining steps): finishing started
  work first is the intent.
- **A batch keeps its turn across customers**: the batch-width admission turn of 0092 is checked before the customer
  rotation, so one document's languages start (and their parked members continue) on consecutive picks even while
  another customer has work.
- **Default quantum 30 s** (`LOC_TMS_STEP_QUANTUM_S`, was 10 s): about two real LLM steps under load, so ordinary
  runs never park; an arriving run of another customer still waits at most about one step plus 30 s.

## Consequences

- One customer flooding the install runs like the pre-S2 queue (runs keep their slots), with the S2 language
  rotation and batch-width admission kept; two customers share slots at step boundaries.
- The rotation state and the parked continuations stay per worker process (S3b/S3c): each worker continues its own
  parked runs before its own admissions, and the wait cap bounds a parked run's wait in its worker.
- Proof: `verify_fair_dispatch` (continuous-arrival starvation, numeric order, wait cap, siblings) and
  `verify_step_queue` (no park before review, slow-mock end to end with steps longer than the quantum). A real-LLM
  flood rerun confirms it separately.
