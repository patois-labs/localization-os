# Vendor-interop research: interchange standards: TAPICC / COTI / XLIFF / others (research notes, 2026-08-08)

> Primary-source based. Feeds both the vendor-portal interop proposal and the parked
> STANDARD-XLIFF-export roadmap item (the 1.2-vs-2.x choice).

## TAPICC (GALA)
- Track 1 produced: a business-object/actor/use-case catalog (terminology wiki for localization API interactions) and an **XLIFF 2 Extraction and Merging Best Practice v1.0** (WG3).
- **Status: DORMANT.** GitHub repo (github.com/GALAglobal/TAPICC) last commits **2018-11-29** - 7+ years stale. GALA: "deprioritized when the global pandemic started in 2020"; no evidence the work was ever carried on.
- **Adoption: none found** in production by any named TMS or LSP vendor. Never progressed past best-practice drafts.
- **Reusable**: as a checklist/vocabulary reference only (did we cover submit-job, accept/decline, status-poll, deliverable-return, LQA-feedback, invoice-trigger?), and the XLIFF2 extraction/merge best practice if XLIFF 2 is ever the payload. Not a wire protocol to implement.

## COTI (DERCOM)
- German CMS/authoring-tool consortium standard for CMS↔TMS/LSP exchange. Three levels:
  - **Level 1** - manual COTI package exchange (structured ZIP + manifest, portal upload/download).
  - **Level 2** - hot-folder automation of the same package format.
  - **Level 3** - full API integration, no package materialized.
- Package = ZIP + structured manifest; the actual XML manifest schema is **not openly published** (documented in DERCOM member/implementer channels; no public spec repo found).
- **Implementers**: RWS Trados (to L3), Across (L2–3), STAR Transit (L3), Kaleidoscope Contentflow, Eurotext. A **German/DACH CCMS-ecosystem convention** - real and alive in its niche, absent from the global TMS market (Smartling, Phrase, memoQ, XTM, Lokalise, Crowdin show no COTI support).
- **Status: alive but regional/vertical**; no IETF/OASIS/W3C ratification.

## XLIFF version landscape
**Adoption reality 2025–2026: XLIFF 1.2 still dominates day-to-day interchange**, despite XLIFF 2.x being technically superior and actively maintained.

| Consideration | XLIFF 1.2 | XLIFF 2.x |
|---|---|---|
| Tooling breadth | Universal - virtually every CAT/TMS/CMS filter | Growing but incomplete; many tools "support" 2.0 via internal down-conversion to 1.2 |
| Apple/Xcode | Required - Xcode has zero XLIFF 2.0 support even in Xcode 16/17; Apple went `.xcstrings` instead | Not usable for iOS pipelines |
| Segment/unit state model | Flat `trans-unit`, coarse `state`/`state-qualifier` - weak workflow granularity | Proper `<segment>`-level state enum (`initial`/`translated`/`reviewed`/`final`) inside `<unit>` - genuine per-segment workflow tracking; materially better for "40 of 60 segments delivered" payloads |
| Metadata/extensibility | Ad hoc `<prop-group>`/custom namespaces, inconsistent | Formal modules: metadata, matches, glossary, size-restriction, change-tracking - structured slots for LQA scores, TM match %, glossary hits |
| Latest ratified | Stable since 2008 | **XLIFF 2.2 became a full OASIS Standard 2025-03-13** - actively maintained line (2.0→2014, 2.1→2018, 2.2→2025) |
| Practical guidance | "Safer default for widest interoperability today" | "Use if your toolchain fully supports it" |

**JLIFF** (JSON serialization of the XLIFF object model, OASIS XLIFF OMOS TC): **pre-standard** - OASIS's own TC page states no approved work products yet. One independent implementation (vistatec/JliffGraphTools). Design influence to track, not adoptable.

**Net read**: XLIFF 1.2 is the safe lowest-common-denominator wire format (every LSP tool opens it); XLIFF 2.x's segment-state machine and metadata modules are the better *internal model* to borrow - emit 1.2 for compatibility, optionally offer 2.x.

## Other efforts
- **OASIS Translation Web Services (TWS)** - 2002 SOAP/WSDL-era; reached Draft Committee Spec 1.0.3 only; genuinely dead.
- **Linport / TIPP** - early-2010s portfolio-package efforts; never reached a standards body; abandoned.
- **Okapi Framework** - not a standard; a widely-used implementation toolkit (filters, pipelines, XLIFF/TMX/SRX/ITS handling); reinforces XLIFF+TMX as the actual common currency.
- **MT-provider APIs** (Google/DeepL/Microsoft/Amazon/ModernMT) - text-in/text-out engines with no job/vendor/PO/deadline/LQA semantics; not a job-interchange layer; their non-reuse for human jobs is further evidence for the no-standard conclusion.

## THE HONEST CONCLUSION (agent's analysis)
**There is no living, broadly-adopted, open standard for client↔vendor translation-job interchange.** Every candidate died (TAPICC, TWS, Linport), survives only as a regional convention (COTI/DACH), or is infrastructure not protocol (Okapi; XLIFF/TMX are *file* formats, never the job/status/vendor-management API layer). The only active ratified work - OASIS XLIFF core, most recently 2.2 (Mar 2025) - governs content serialization, not job lifecycle. Every commercial TMS/LSP with a public API ships **proprietary REST + JSON**, each with its own job/file/segment/status model, with XLIFF (usually 1.2) relegated to an importable/exportable file format.

**Recommendation for a new TMS vendor-portal wire format:**
1. Build the API as bespoke REST/JSON (job, file, deliverable, status, LQA-score resources) - do not implement TAPICC/COTI/TWS as the protocol.
2. Use **XLIFF 1.2 as the lowest-common-denominator export/import format** for content payloads - the one piece of "industry standard" reality worth honoring.
3. **Borrow XLIFF 2.x's segment-level state enum and modular metadata design as the internal/JSON job-status model**; offering an XLIFF 2 export as an option later costs little once the internal model mirrors it.
4. If ever targeting German enterprise-CCMS specifically, COTI Level 2 (hot-folder ZIP) is a real integration point - a market-specific connector, not an architecture driver.
5. Track OASIS XLIFF OMOS/JLIFF loosely; do not build against it today.

## Sources
- https://github.com/GALAglobal/TAPICC
- https://github.com/GALAglobal/TAPICC/wiki
- https://galaglobal.github.io/TAPICC/T1/WG3/prd01/XLIFF-EM-BP-V1.0-prd01.pdf
- https://www.gala-global.org/knowledge-center/industry-development/TAPICC
- https://eurotext.de/en/technology/coti/
- https://www.star-deutschland.net/en/how-translations-can-be-processed-faster-with-coti-level-3/
- https://de.wikipedia.org/wiki/COTI
- https://kaleidoscope.at/en/translation/experttools-small-but-mighty/coti
- https://help.memsource.com/hc/en-us/articles/360012112880-Common-Translation-Interface-COTI-
- https://docs.oasis-open.org/xliff/xliff-core/v2.2/xliff-core-v2.2-part1.html
- https://www.oasis-open.org/committees/xliff-omos/
- https://github.com/oasis-tcs/xliff-omos-jliff
- https://locize.com/file-formats/xliff/
- https://support.phrase.com/hc/en-us/articles/5709613119516--XLIFF-1-2-and-2-0-XML-Localization-Interchange-File-Format-TMS
- https://en.wikipedia.org/wiki/XLIFF
- http://www.oasis-open.org/committees/tc_home.php?wg_abbrev=trans-ws
- https://okapiframework.org/wiki/index.php/Main_Page
