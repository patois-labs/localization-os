"""FULL_TEXT prompt offers. No guarantee: use SEGMENTED when wording must be exact.

Documented in: docs/architecture/translation-pipeline.md
"""


def _san_offer(text) -> str:
    """Newline-flatten + neutralize angle brackets so a TM value can never forge a closing
    `</tm_offers>` tag or a column-0 structure line.

    THE SAME defense `lqa_downstream_findings_bloc._san_finding` applies to findings, for
    the same reason and against a wider door: TM is writable by a translator, by a TMX
    import and by a connector sync from Airtable or Jira, so an offer string is
    attacker-reachable at a much lower privilege than a judge's own note. Without this a
    target containing `</tm_offers>` closes the block and everything after it reads as
    top-level prompt. Full-width homoglyphs are neutralized too, or a homoglyph fence
    renders instead.
    """
    s = " ".join(str(text if text is not None else "").split())
    return s.replace("<", "&lt;").replace(">", "&gt;").replace("＜", "&lt;").replace("＞", "&gt;")


#: Why a redacting step gets no offers at all. See `render_offers`.
REDACTED_STEP_NOTE = (
    "TM offers withheld: this step redacts PII, and an offer reproduces the document's own "
    "sentence verbatim, so it cannot be offered without either leaking the text the "
    "redaction just removed or naming placeholders this prompt does not define."
)


def render_offers(offers: list[dict]) -> str:
    """The offer block, sanitized. NEVER call this on a step that redacts PII.

    An offer reproduces the document's own sentence verbatim, which is exactly what a
    redacting step is removing, so the two cannot both be honoured in one prompt:

      * emit the offer raw and the sentence `{text}` just had scrubbed goes to the engine in
        clear, defeating the redaction on precisely the runs that asked for it;
      * redact the offer separately and it gets its OWN placeholder namespace --
        `pii.redact` restarts its counter at 1 on every call and takes no seed map -- so the
        offer's `«PII_EMAIL_1»` denotes a different value than the body's. `pii.restore`
        resolves the engine's output against the BODY's map alone, so on a document with two
        PII values the delivered translation carries one subject's data inside another
        subject's sentence. That is worse than not offering, and it is not hypothetical: it
        was measured before this was changed.

    So the caller skips the block entirely when `redact` is on and records
    `REDACTED_STEP_NOTE`. Offering under redaction is a train-2 problem: it needs `pii` to
    accept a shared map, which is a change to a security seam and does not belong in a
    feature train.
    """
    if not offers:
        return ""
    rows = [
        f"  - {_san_offer(row.get('source', ''))}  =>  {_san_offer(row.get('target', ''))}"
        for row in offers
    ]
    return (
        "<tm_offers>\n"
        "Approved existing translations. Where the source contains these sentences, reproduce the\n"
        "approved target EXACTLY and do not re-word it. Translate everything else normally.\n"
        + "\n".join(rows)
        + "\n</tm_offers>"
    )
