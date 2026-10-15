"""Shared CLI helpers: output (human vs --json), errors, exit codes,
confirmation prompts, and secret redaction.

Exit codes (the whole CLI honours these):
    0  success
    1  operation failure (the thing the operator asked for did not work)
    2  usage error (argparse's default for bad arguments)
    3  remote unreachable (reserved for the later --remote HTTP mode)

Documented in: docs/architecture/surfaces-and-parity.md
"""

import argparse
import contextlib
import json
import os
import sys


def same_file(a, b) -> bool:
    """True when two paths name the SAME file (#525). A bare abspath comparison is not
    enough on this repo's primary host: NTFS is case-insensitive, and abspath resolves
    neither symlinks nor 8.3 short names, so a case-variant of the live DB path looked
    different and a `--force` then deleted the SOURCE. Two checks, either one is a match:
    os.path.samefile (file identity, when both exist, which also catches hard links) and
    a case-folded realpath comparison (which still works when the destination does not
    exist yet)."""
    a, b = os.fspath(a), os.fspath(b)
    try:
        if os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3

# SQLite's binder rejects any integer outside a signed 64-bit range with a raw
# OverflowError deep in the driver, well past any friendly error handling. Every
# id-taking CLI argument is `type=int` today, so a huge value (a copy/paste typo,
# a fuzzer, an off-by-orders-of-magnitude script bug) reaches the DB layer and
# crashes with a traceback instead of a clean usage error (bug #7). Ids are also
# always positive (autoincrement primary keys start at 1), so 0 and negative
# values are rejected too - a friendlier message than the eventual "not found".
_SQLITE_INT64_MAX = 2**63 - 1


def projected_entries_sentence(n: int) -> str:
    """The shared CLI sentence for a concept-preserving import's ``projected_entries``
    counter (round-4 M2). ONE definition, used by both ``cmd_project`` (local) and
    ``_remote_impl`` (--remote), so the two arms print byte-identically and match the
    prose the operator reads in docs/CLI.md, help.html and capabilities/glossary.md: the
    counter is what THIS import's terms enforce for the glossary's pair(s), not a
    whole-glossary total (a re-import reports the subset its own terms cover). Pre-fix both
    surfaces printed "N entr(y|ies) project for the glossary's language pair(s)." -- the
    whole-glossary phrasing the L1 wording fix had already retired from the descriptors."""
    return (
        f" {n} entr{'y' if n == 1 else 'ies'} this import's terms enforce for the "
        "glossary's language pair(s)."
    )


def bounded_id(value: str) -> int:
    """argparse `type=` for a positive, in-range integer id. Use this instead of
    the bare `type=int` for every id-taking CLI argument (project_id, doc_id,
    customer_id, ...): a non-integer, non-positive, or out-of-range value fails
    with a clean argparse usage error (exit 2) rather than a raw OverflowError
    surfacing from the SQLite binder several layers down."""
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"id must be a number, got {value!r}.") from e
    if n <= 0:
        raise argparse.ArgumentTypeError(f"id must be a positive number, got {value!r}.")
    if n > _SQLITE_INT64_MAX:
        raise argparse.ArgumentTypeError(f"id is too large, got {value!r}.")
    return n


def bounded_cursor(value: str) -> int:
    """Use the shared ID bound with a cursor-specific usage error."""
    try:
        return bounded_id(value)
    except argparse.ArgumentTypeError as exc:
        raise argparse.ArgumentTypeError("cursor must be a positive integer") from exc


def digit_ref_to_id(digits: str) -> int | None:
    """Bound a digit-only string (from an "email or numeric id" free-form CLI
    argument, e.g. `user show <ref>`) to a valid SQLite id, or None when it is
    out of range. Callers that already know `digits.isdigit()` use this instead
    of a bare `int(digits)` so an oversized value (e.g. a 20-digit typo) resolves
    to "not found" rather than crashing the SQLite binder with a raw
    OverflowError several layers down (mirrors bounded_id, but for the
    email-or-id helpers that can't use an argparse `type=`).

    NEVER raises: callers gate on `digits.isdigit()`, but str.isdigit() is True for a
    Unicode digit int() rejects (e.g. the superscript '2', U+00B2 -> ValueError), so the
    int() itself must be guarded -- a malformed ref resolves to None ("not found"), not a
    500/traceback."""
    try:
        n = int(digits)
    except (TypeError, ValueError):
        return None
    if n <= 0 or n > _SQLITE_INT64_MAX:
        return None
    return n


def _session():
    """A fresh in-process DB session (the app engine, honouring DATA_DIR)."""
    from sqlmodel import Session

    from app.db import engine

    return Session(engine)


def default_out_in_cwd(name: str) -> str:
    """The path a DEFAULT (no `-o`) export writes to, proven to be inside the CWD.

    Issue #142, the sink half. Export verbs compose their default name from the STORED
    document filename (`<stem>.<locale>.<fmt>`), and a row renamed before the rename
    guard landed can still carry `../../../../Windows/win.ini.txt` -- there is no
    migration. Callers compose the name through content.safe_output_name first (basename +
    a length bound); this is the buckle, and it answers ONE question: does the resolved
    path sit directly in the current working directory? A name that escapes -- including
    a single component that is itself a symlink or junction pointing elsewhere -- is
    refused with a clean one-line error rather than written.

    It does NOT promise the path is openable: the length bound lives in safe_output_name
    and the sinks turn any remaining OSError from open() into a clean CliError.

    An EXPLICIT `-o` never comes here: a path the operator typed is their own choice."""
    import os as _os

    cwd = _os.path.realpath(_os.getcwd())
    resolved = _os.path.realpath(_os.path.join(cwd, name))
    if _os.path.dirname(resolved) != cwd:
        raise CliError(
            f"refusing to write the default export outside the current directory ({name!r}); "
            "pass -o/--out with the path you want.",
            EXIT_USAGE,
        )
    return name


def get_scoped_or_404(obj, obj_id, label, org_id=None):
    """Enforce the in-process --org scope on an already-loaded row (the load itself
    varies per caller: session.get, a service getter, ...). A missing OR cross-org
    row is indistinguishable and both become a clean 404-as-usage (exit 2):
    Cross-org (in-process --org) is indistinguishable from missing. `label` is the
    noun in the message ("project" -> "no such project: 5"). Returns `obj`."""
    if obj is None or (org_id is not None and getattr(obj, "org_id", None) != org_id):
        raise CliError(f"no such {label}: {obj_id}", EXIT_USAGE)
    return obj


# --- workflow-step table labels (shared by the in-process `workflow show` and the
# --- --remote mirror in _remote_impl; kept here so both render byte-identically). --
def _branch_label(branch) -> str:
    """A compact ASCII branch summary for the CLI table (Windows-safe). '' = no branch."""
    if not isinstance(branch, dict):
        return ""

    def _t(v):
        if isinstance(v, dict) and "step" in v:
            return "->" + str(v["step"])
        return str(v)

    return " ".join(
        f"{slot[3:]}:{_t(branch[slot])}" for slot in ("on_pass", "on_fail") if slot in branch
    )


def _opts_label(s) -> str:
    """Compact ASCII summary of the engine-layer flags (F2/TM-examples) that deviate
    from the byte-identical default, for the `workflow show` human table. '-' when
    every flag is at its default (adaptive off, doc-context off, tm-examples on at
    the default window) — the common case for most steps."""
    parts = []
    if s.get("adaptive_feedback"):
        parts.append(f"adaptive:{s.get('adaptive_feedback_window')}")
    if s.get("doc_context_prepass"):
        parts.append("doc-ctx")
    if not s.get("tm_examples", True):
        parts.append("no-tm-ex")
    elif s.get("tm_examples_window") != 3:
        parts.append(f"tm-ex:{s.get('tm_examples_window')}")
    # LQA safety floor D1: the TM provenance guard's per-step override. None (the
    # correction-loop-shaped default) prints nothing — only a deliberate always/never
    # override is a deviation worth showing.
    if s.get("tm_protect") is True:
        parts.append("tm-protect:on")
    elif s.get("tm_protect") is False:
        parts.append("tm-protect:off")
    # run-190 Item D / M5: the over-translation (page-dump) guard opt-out. None (inherit the
    # install guard) prints nothing; only a deliberate on/off override is a deviation shown.
    if s.get("over_translation_guard") is True:
        parts.append("ot-guard:on")
    elif s.get("over_translation_guard") is False:
        parts.append("ot-guard:off")
    # LLMemory step-level opt-out (PROPOSAL-llmemory §6): same convention -- None/
    # "auto" (participate normally) prints nothing; only the deliberate "off"
    # override is a deviation worth showing.
    if s.get("memory_mode") == "off":
        parts.append("memory:off")
    # feat/lqa-configurable-threshold: an lqa step's configured pass bar. None (the
    # built-in default 90) prints nothing -- only a deliberate override is a deviation
    # worth showing (e.g. "lqa-floor>=94"). "floor" because a major/critical MQM error
    # routes to review regardless of score (Q7 discoverability).
    if s.get("lqa_threshold") is not None:
        parts.append(f"lqa-floor>={s.get('lqa_threshold')}")
    return " ".join(parts) or "-"


def _prompt_snippet(text, template_id, limit=40) -> str:
    """A Windows-console-safe truncated preview of a step's inline prompt for the
    human table (--json carries the full `prompt_text`). A step that uses a saved
    prompt template (no inline text) shows the template id instead of blank."""
    text = " ".join((text or "").split())
    if not text:
        return f"(template #{template_id})" if template_id else "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_package_view(emit, steps) -> None:
    """Print the per-step PACKAGE / VERIFY block for `workflow show` (human mode),
    single-sourced from package_view (SPEC-workflows-redesign Track B, B0). `steps` is
    the list of step ROW dicts the show path already built, each carrying `package` and
    `verify` fact lists ({label, value, scope}). Shared by the in-process `_wf_show` and
    the `--remote` `workflow_show` mirror so the two arms print the same block.

    ASCII + aligned (Windows-console safe); a fact resolved per document / per language
    pair prints its honest scope tag in parentheses; a `-` never appears (package_view
    omits an absent thing entirely). A no-op under --json/--quiet (emit.say guards)."""
    if not any(s.get("package") or s.get("verify") for s in steps):
        return
    from app.kernel.package_view import SCOPE_NOTE

    emit.say("")
    emit.say("Per-step package & verification:")
    for n, s in enumerate(steps, 1):
        pkg = s.get("package") or []
        vf = s.get("verify") or []
        w = max((len(f["label"]) for f in pkg + vf), default=0)
        emit.say("")
        emit.say(f"  Step {n}: {s.get('name') or s.get('type')} ({s.get('type')})")
        for tag, facts in (("PACKAGE", pkg), ("VERIFY", vf)):
            first = True
            for f in facts:
                scope = f.get("scope")
                suffix = f"  ({SCOPE_NOTE[scope]})" if scope in SCOPE_NOTE else ""
                lead = tag if first else ""
                emit.say(f"    {lead.ljust(8)}{f['label'].ljust(w)}  {f['value']}{suffix}")
                first = False


def render_paragraph_view(emit, doc_id, locale: str, view: dict) -> None:
    """Print the FULL_TEXT per-paragraph diagnosis block (`doc paragraphs`, human mode).

    Shared by the in-process `cmd_doc._doc_paragraphs` and the `--remote`
    `_remote_impl.doc_paragraphs` mirror so the two arms print the same block from the
    same `split_view` payload (the three #42 recovery verbs are --remote-capable, like
    `doc export --allow-source-fallback`).

    Reads ONLY the seam's payload — no DB, no import of the service layer beyond the
    ASCII token formatter — so the remote arm can render a server's JSON verbatim."""
    from app.kernel.quality import ascii_reason

    emit.say(
        "Document #%s (%s) - %d paragraph(s), %d damaged."  # noqa: UP031
        % (doc_id, locale, len(view["rows"]), view["damaged_count"])
    )
    if view.get("reverted_indices"):
        emit.say(
            "  %d paragraph(s) already reverted to SOURCE (%.0f%% of the body "  # noqa: UP031
            "is UNTRANSLATED): %s"
            % (
                len(view["reverted_indices"]),
                view["fallback_share"] * 100.0,
                ", ".join(str(i) for i in view["reverted_indices"]),
            )
        )
    for r in view["rows"]:
        if not r["damaged"]:
            continue
        # `reason` FIRST and unconditionally: the multiset-preserving damage classes -
        # an emptied paired span, broken nesting - have an EMPTY missing/extra delta, so
        # a line built from the delta alone printed "paragraph 0: " and named nothing.
        bits = [ascii_reason(r.get("reason")) or "inline tag damage"]
        if r["missing"]:
            bits.append("missing [{}]".format(ascii_reason(", ".join(r["missing"]))))
        if r["extra"]:
            bits.append("unexpected [{}]".format(ascii_reason(", ".join(r["extra"]))))
        emit.say("  paragraph %d: %s" % (r["index"], "; ".join(bits)))  # noqa: UP031
    if view["damaged_count"]:
        emit.say(
            f"  Fix with:  loc-tms doc set-paragraph --doc {doc_id} --locale {locale} "
            "--index N --from-file p.txt"
        )
        emit.say(
            f"  Or recover:  loc-tms doc revert-paragraph --doc {doc_id} --locale {locale} "
            "--index N   (that paragraph ships UNTRANSLATED)"
        )


def _actor(args) -> str:
    """The audit actor for a CLI mutation: the explicit --actor if given, else 'cli'."""
    return getattr(args, "actor", None) or "cli"


def _resolve_user(session, ref: str):
    """Look a user up by numeric id OR email. Raises a clean CliError if not
    found (exit 2 — a bad argument, not an op failure).

    An oversized digit string (e.g. a copy/paste typo) resolves to not-found via
    digit_ref_to_id rather than reaching session.get with a raw int() that
    overflows the SQLite binder (bug: OverflowError on `user show <huge>`)."""
    from sqlmodel import select

    from app.models import User

    ref = (ref or "").strip()
    if ref.isdigit():
        uid = digit_ref_to_id(ref)
        u = session.get(User, uid) if uid is not None else None
    else:
        u = session.exec(select(User).where(User.email == ref.lower())).first()
    if u is None:
        raise CliError(f"no such user: {ref}", EXIT_USAGE)
    return u


class CliError(Exception):
    """A clean, expected failure. __main__ turns this into a message on stderr
    plus the given exit code (default 1) - no traceback.

    `payload` is an OPTIONAL dict of extra machine-readable fields (e.g. a precise
    outcome status). When set, __main__ MERGES it into the `--json` error object
    (never overriding `ok`/`error`), so a scripted caller can branch on structured
    fields instead of parsing the human message. Ignored in human mode."""

    def __init__(self, message: str, code: int = EXIT_FAIL, *, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


@contextlib.contextmanager
def quiet_import_log():
    """Keep tracebacks out of the terminal while an in-process glossary import runs (#615).

    The importer logs the full error of a failed chunk with its traceback so a server operator
    can diagnose it, but the CLI has no log file: Python's last-resort handler would print that
    traceback, SQL and bound row values included, on the operator's terminal. This drops the
    traceback from records of the importer's logger for the duration of the call."""
    import logging

    class _NoTrace(logging.Filter):
        def filter(self, record):
            record.exc_info = None
            record.exc_text = None
            return True

    log = logging.getLogger("app.services.glossary_hub")
    flt = _NoTrace()
    log.addFilter(flt)
    try:
        yield
    finally:
        log.removeFilter(flt)


def glossary_import_cli_error(exc: Exception) -> CliError:
    """The CliError for a glossary import (project or module) that raised (#615).

    A big import that stopped after some chunks were stored keeps them: the message (class name
    and counts only) says how far it got, and `--json` carries `partial`, `rows_written` and
    `rows_total`. A database error's text carries SQL and bound row values (glossary terms), so
    it is logged and replaced by a generic line. Anything else (bad header, bad TBX, row cap)
    keeps its own user-facing message."""
    from sqlalchemy.exc import SQLAlchemyError

    if hasattr(exc, "rows_written") and hasattr(exc, "rows_total"):
        return CliError(
            f"could not import glossary: {exc}",
            payload={
                "partial": True,
                "rows_written": exc.rows_written,
                "rows_total": exc.rows_total,
            },
        )
    if isinstance(exc, SQLAlchemyError):
        return CliError(
            "could not import glossary: a database error stopped the import; nothing was "
            "stored. Try again; if it repeats, check the application log."
        )
    return CliError(f"could not import glossary: {exc}")


# Substrings that mark a settings/env key as a secret. A key matching any of
# these is never echoed in plaintext by the CLI - get/list show "(set)"/"(unset)".
_SECRET_MARKERS = ("password", "secret", "client_secret", "api_key", "_enc", "token")
# Credential-material keys whose NAME carries no marker above (R3 review HIGH:
# operator_key_hash is a crackable digest). Mirrors settings._EXPLICIT_SECRET_KEYS;
# keep the two in sync so `config list` masks the same keys the /api read does.
_EXPLICIT_SECRET_KEYS = ("operator_key_hash",)


# Password-policy keys: "password" in the NAME, a plain number in the VALUE. Mirrors
# settings.NOT_SECRET_KEYS exactly (verify_password_policy pins the two).
NOT_SECRET_KEYS = (
    "password_min_length",
    "password_require_classes",
    "password_block_common",
    "password_history",
    "password_max_age_days",
)


def is_secret_key(key: str) -> bool:
    if key in NOT_SECRET_KEYS:
        return False
    k = (key or "").lower()
    return key in _EXPLICIT_SECRET_KEYS or any(m in k for m in _SECRET_MARKERS)


def redact(key: str, value) -> str:
    """Render a value for display, masking it entirely when the key is a secret.
    Secrets show '(set)' / '(unset)' - never a partial reveal. An endpoint-URL key
    (settings.is_redacted_url_key) is PARTIALLY redacted - scheme+host+path kept,
    embedded userinfo/query stripped - so a credential in the URL never echoes. The
    redaction logic is the kernel's single source of truth (imported lazily to keep
    _common import-light); this mirror covers any CLI surface that renders through
    redact() rather than cmd_config._display_value."""
    if is_secret_key(key):
        return "(set)" if (value not in (None, "", "(unset)")) else "(unset)"
    if value is None:
        return ""
    from app.kernel import settings as _settings

    if _settings.is_redacted_url_key(key):
        return _settings.redact_url_value(str(value))
    return str(value)


def _write_ascii_safe(stream, text: str) -> None:
    """Write `text` to `stream`, degrading gracefully instead of crashing when the
    console's codec cannot encode a character (bug: a rename to a non-ASCII name like
    "Wörkflöw -> ünïcode" committed to the DB + audit, then the CLI died with a raw
    UnicodeEncodeError on a cp1252 Windows console -- nonzero exit + traceback AFTER
    the operation had already succeeded).

    The DB / audit values are untouched: this only shapes terminal ECHO. On a codec
    failure the offending characters are backslash-escaped (errors="backslashreplace")
    into a form the stream's own encoding CAN represent, so every existing command's
    output is safe on any console, not just the new workflow ones. A stream that
    accepts arbitrary unicode (a StringIO capture, a utf-8 console) takes the direct
    path unchanged."""
    try:
        stream.write(text)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(enc, "backslashreplace").decode(enc, "replace"))


class Emitter:
    """Bundles the global output flags so commands stay terse. One instance is
    created in __main__ from the parsed args and passed to every command."""

    def __init__(self, as_json: bool = False, quiet: bool = False, verbose: bool = False):
        self.as_json = as_json
        self.quiet = quiet
        self.verbose = verbose

    # --- machine output -------------------------------------------------
    def json(self, payload) -> None:
        """Emit a JSON object (used when --json is set). Always goes to stdout."""
        _write_ascii_safe(sys.stdout, json.dumps(payload, default=str) + "\n")

    # --- human output ---------------------------------------------------
    def say(self, message: str = "") -> None:
        """A normal line of human output. Suppressed by --quiet and by --json
        (machine consumers get the json() payload instead)."""
        if self.quiet or self.as_json:
            return
        _write_ascii_safe(sys.stdout, message + "\n")

    def detail(self, message: str) -> None:
        """Extra context, only shown under --verbose."""
        if self.verbose and not self.as_json and not self.quiet:
            _write_ascii_safe(sys.stdout, message + "\n")

    def warn(self, message: str) -> None:
        """A non-fatal warning. Always to stderr so it never pollutes --json
        stdout; suppressed by --quiet."""
        if self.quiet:
            return
        _write_ascii_safe(sys.stderr, "warning: " + message + "\n")

    # --- structured helpers --------------------------------------------
    def result(self, human: str, payload: dict) -> None:
        """The common success shape: one human line OR a json object, per the
        active mode. `payload` should always carry {"ok": True, ...}."""
        if self.as_json:
            self.json(payload)
        else:
            self.say(human)

    def table(self, rows, headers=None) -> None:
        """Aligned columnar output for human mode. `rows` is a list of tuples of
        strings; `headers` an optional tuple. No-op under --json (callers emit a
        list payload themselves) and under --quiet."""
        if self.as_json or self.quiet:
            return
        rows = [tuple(str(c) for c in r) for r in rows]
        if headers:
            rows = [tuple(str(h) for h in headers)] + rows  # noqa: RUF005
        if not rows:
            return
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for idx, row in enumerate(rows):
            line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
            _write_ascii_safe(sys.stdout, line.rstrip() + "\n")
            if headers and idx == 0:
                _write_ascii_safe(sys.stdout, "  ".join("-" * w for w in widths) + "\n")


def confirm(emit: Emitter, prompt: str, assume_yes: bool) -> bool:
    """Ask a destructive-op yes/no question. --yes (assume_yes) skips the prompt.
    In --json/--quiet mode with no --yes, we REFUSE (return False) rather than
    block on stdin - a machine caller must pass --yes explicitly."""
    if assume_yes:
        return True
    if emit.as_json or emit.quiet or not sys.stdin.isatty():
        emit.warn(
            "refusing a destructive action without --yes (non-interactive / --json / --quiet)."
        )
        return False
    try:
        answer = input(prompt + " [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    return answer in ("y", "yes")


def feature_gate(slug: str) -> None:
    """The layer-2 CLI kill switch for a feature's verbs: refuse with the feature's own
    `cli_refusal` message and `cli_exit_code` when its switch is off. The ONE home of the
    refusal string (it used to be hand-copied into nine modules that had to stay
    byte-identical to the registry); `verify_features` check 1b pins every gate to it."""
    from app import features

    if features.is_enabled(slug):
        return
    feat = features.feature_by_slug(slug)
    if feat is None:  # an unknown slug (a paid feature whose add-on is absent): refuse cleanly
        raise CliError(f"{slug} is not available in this edition.", 2)
    msg = feat.cli_refusal or f"{feat.title.lower()} is disabled (setting {feat.setting_key}=0)."
    raise CliError(msg, feat.cli_exit_code)


TERM_META_FLAGS = ("part_of_speech", "definition", "usage_example", "subject_field", "notes")


def term_meta_from_args(args) -> dict:
    """Collect the provided term-metadata flags into a dict (skips None = unspecified;
    keeps "" so an edit can CLEAR a field). The engine sanitizes + allowlists. ONE copy for
    the in-process arms and the --remote arms (#533)."""
    out = {}
    for f in TERM_META_FLAGS:
        v = getattr(args, f, None)
        if v is not None:
            out[f] = v
    return out


def get_customer(session, customer_id, org_id=None):
    """A customer inside the operator's --org scope, or the clean 404-as-usage (exit 2): a
    nonexistent or cross-org customer never falls back to "all visible" and leaks nothing."""
    from app.models import Customer

    return get_scoped_or_404(session.get(Customer, customer_id), customer_id, "customer", org_id)


def parse_kv(pairs, what: str) -> dict:
    """Parse a list of 'k=v' strings into a dict; blank values are allowed but a missing
    '=' or an empty key is a usage error."""
    out = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise CliError(f"bad {what} (expected KEY=VALUE): {raw!r}", EXIT_USAGE)
        key, val = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise CliError(f"bad {what} (empty key): {raw!r}", EXIT_USAGE)
        out[key] = val
    return out


def parse_secret_kv(pairs, what: str) -> dict:
    """`parse_kv` for a credential flag: `KEY=-` reads the value from one stdin line (never
    echoed, never on argv), the same convention as `connector create --secret api_key=-`.
    Any other value is kept as given. An empty stdin line is a usage error: an update would
    otherwise keep the old key and a create would store none, both silently."""
    out = parse_kv(pairs, what)
    for key, val in list(out.items()):
        if val == "-":
            out[key] = sys.stdin.readline().rstrip("\r\n")
            if not out[key].strip():
                raise CliError(f"{what} {key}=- read an empty value from stdin.", EXIT_USAGE)
    return out


def apikey_expiry_cell(k: dict) -> str:
    """The `expires` cell of `apikey list` (#270): the expiry date, 'never' for a no-expiry
    key, and the 'expired' state when the date has passed. Shared by the in-process and
    --remote arms so the two print byte-identical tables."""
    if not k.get("expires_at"):
        return "never"
    cell = k["expires_at"][:10]
    return f"{cell} (expired)" if k.get("expired") else cell


def spend_estimate(
    provider,
    model,
    *,
    calls: int,
    input_tokens: int,
    output_tokens: int,
    jev_input_tokens: int = 0,
    note: str = "",
) -> dict:
    """The pre-flight number for a CLI verb that makes paid provider calls (#531).

    Priced through the ONE shared rule (`spend.cost_for_ref`): a free engine (mock, a private
    local endpoint) is $0, everything else prices by model. `jev_input_tokens` is billed at
    the hosted Jev judge's per-token rate. Token counts are character-derived estimates, so
    the figure is an estimate, calibrated to under-surprise rather than to be exact."""
    from app import config
    from app.kernel import qe_jev, spend

    model = model or config.DEFAULT_MODELS.get(provider) or ""
    llm_usd = spend.cost_for_ref(
        provider, model, {"input_tokens": int(input_tokens), "output_tokens": int(output_tokens)}
    )
    jev_usd = int(jev_input_tokens) * qe_jev.JEV_USD_PER_INPUT_TOKEN
    total = round(llm_usd + jev_usd, 6)
    return {
        "provider": provider,
        "model": model,
        "calls": int(calls),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "llm_usd": round(llm_usd, 6),
        "jev_usd": round(jev_usd, 6),
        "total_usd": total,
        "free": total == 0,
        "note": note,
    }


def spend_gate(emit: Emitter, args, estimate: dict, *, verb: str) -> bool:
    """The estimate-then-`--yes` gate `eval run start` and `eval meta` use, for any verb that
    spends on a provider (#531). The estimate is printed EITHER WAY; without --yes nothing
    runs (even on a free engine: the unconfirmed call runs nothing, as an invariant), the
    --json payload says `confirmed: false`, and the caller returns 0. True = go ahead."""
    price = (
        "free (no billable engine)"
        if estimate["free"]
        else "~${:.4f}".format(estimate["total_usd"])
    )
    emit.say(
        "Estimate for {}: {} provider call(s) on {}, ~{} in / ~{} out tokens, {}.".format(
            verb,
            estimate["calls"],
            estimate["provider"],
            estimate["input_tokens"],
            estimate["output_tokens"],
            price,
        )
    )
    if estimate.get("note"):
        emit.say("  " + estimate["note"])
    if getattr(args, "yes", False):
        return True
    emit.say("Re-run with --yes to confirm the estimate and run.")
    emit.result("", {"ok": True, "confirmed": False, "estimate": estimate})
    return False


def spend_gate_remote(emit: Emitter, args, *, verb: str, detail: str) -> bool:
    """The --remote twin of spend_gate: the dollar figure is priced on the server (the CLI has
    no access to its documents or engines), so say what will run and still require --yes."""
    emit.say(f"{verb} makes paid provider calls on the server: {detail}.")
    if getattr(args, "yes", False):
        return True
    emit.say("Re-run with --yes to confirm and run.")
    emit.result("", {"ok": True, "confirmed": False, "estimate": None, "detail": detail})
    return False


def refuse_over_budget(project_id, args) -> None:
    """The per-department and per-project USD caps `doc translate` enforces, for the other
    in-process verbs that spend against a project's document (#531). Refuses (exit 1) with
    the budget's own message BEFORE any provider call."""
    from app.kernel import budgets, project_budgets
    from app.models import Project

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        dept = budgets.department_for_actor(session, actor)
        allowed, _remaining, msg = budgets.check(dept, org_id=op_org_id(args))
        if not allowed:
            raise CliError(msg)
        if project_id is not None and session.get(Project, project_id) is not None:
            p_allowed, _rem, p_msg = project_budgets.project_check(project_id)
            if not p_allowed:
                project_budgets.evaluate_thresholds(project_id, actor=actor)
                raise CliError(p_msg)


def require_local(args) -> None:
    """Legacy no-server guard, now the SEAM for the two-mode CLI.

    In P6 a command that supports --remote calls remote_mode()/dispatch() instead
    of this. require_local() is kept for the IN-PROCESS-ONLY operator commands
    (db/health/audit-verify/retention-purge/anything touching the DB file or
    keyfiles): if --remote was passed to one of those, fail cleanly with exit 2 so a
    remote invocation is refused rather than silently running against the local host.
    """
    if getattr(args, "remote", None):
        raise CliError(
            "not available over --remote (this is an in-process operator command "
            "that runs on the host; drop --remote to run it locally).",
            EXIT_USAGE,
        )


# --- P6: remote mode + SaaS in-process operator gate -------------------------
def local_op(args, emit) -> None:
    """Convenience for the in-process-ONLY operator commands that touch tenant data
    (users/apikeys/members/admin): refuse --remote (exit 2) AND enforce the SaaS
    in-process operator gate (require_org_scope). A no-op beyond require_local while
    single-tenant, so gate-off behavior is byte-identical."""
    require_local(args)
    require_org_scope(args, emit)


def remote_mode(args) -> bool:
    """True when the caller asked for the thin HTTP client of /api (--remote URL).
    Org scoping in remote mode is done ENTIRELY server-side by the token, so a
    remote command is never super-admin and never touches the local DB."""
    return bool(getattr(args, "remote", None))


def _read_token_file(path: str) -> str:
    """The API key from a --token-file: the file's first line, stripped (issue
    #264). An unreadable file is a clean usage error; an empty first line yields
    '' (the caller's missing-key error names all three sources)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        lines = data.decode(encoding).splitlines()
        token = lines[0].strip() if lines else ""
        # HTTP header values must be ASCII, even when the file itself is Unicode.
        token.encode("ascii")
        return token
    except (UnicodeError, ValueError) as exc:
        raise CliError(
            f"cannot read --token-file {path}: not a UTF-8/UTF-16 text file with an ASCII key",
            EXIT_USAGE,
        ) from exc
    except OSError as exc:
        raise CliError(f"cannot read --token-file {path}: {exc}", EXIT_USAGE) from exc


def remote_client(args):
    """Build the httpx-backed /api client from --remote + the key, read in
    priority order (issue #264): --token, then --token-file PATH (first line,
    stripped), then the LOC_TMS_TOKEN env var. A --remote with no key by any of
    the three is a usage error (every /api route is token-authed). A key given
    on argv (--token) is visible in process listings and shell history, so its
    use prints ONE stderr warning line naming the two argv-free alternatives.
    --insecure is forwarded to RemoteClient's plain-HTTP host guard."""
    from ._remote import RemoteClient

    url = getattr(args, "remote", None)
    token = getattr(args, "token", None)
    if token:
        sys.stderr.write(
            "warning: --token is visible in process listings and shell history; "
            "prefer the LOC_TMS_TOKEN env var or --token-file.\n"
        )
    else:
        token_file = getattr(args, "token_file", None)
        token = (
            _read_token_file(token_file)
            if token_file
            else os.environ.get("LOC_TMS_TOKEN", "").strip()
        )
    if not token:
        raise CliError(
            "--remote requires --token, --token-file PATH, or the LOC_TMS_TOKEN "
            "env var (an lsu_ per-user API key).",
            EXIT_USAGE,
        )
    return RemoteClient(
        url,
        token,
        insecure=bool(getattr(args, "insecure", False)),
        idempotency_key=getattr(args, "idempotency_key", None),
    )


def require_org_scope(args, emit) -> None:
    """The SaaS in-process operator gate (P6, the CLI security matrix).

    A strict NO-OP unless multi-org is active AND we are running IN-PROCESS (no
    --remote). When it IS active, an in-process command that would touch/sweep
    cross-org data must be scoped, mirroring the retention-purge --yes precedent:

      * --org <id>        scopes the whole command to one org (and stamps new rows);
      * --operator        (backed by env LOC_TMS_OPERATOR_KEY, verified against the
                          stored operator_key_hash) authorizes an UNSCOPED cross-org
                          command.

    Without either, an in-process command in multi-org mode is REFUSED (exit 2) so a
    customer/cron on a shared SaaS host cannot silently sweep every org. With the
    gate OFF (single-tenant) this returns immediately — in-process CLI works exactly
    as today, no --org needed, byte-identical.

    On success it records the resolved scope on args (args._op_org_id) for callers
    that stamp/scope; None means "operator-authorized, unscoped cross-org".
    """
    # Remote mode is org-scoped by the token server-side — this gate never applies.
    if remote_mode(args):
        return
    from app.kernel import access

    if not access.multi_org_active():
        args._op_org_id = None
        args._op_unscoped = False
        return
    org = getattr(args, "org", None)
    operator = getattr(args, "operator", False)
    if org is not None:
        args._op_org_id = _resolve_org_scope(org)
        args._op_unscoped = False
        return
    if operator:
        _verify_operator_key()
        args._op_org_id = None  # authorized cross-org, unscoped
        # #73: record that this None is the AUTHORIZED operator door, not a broken
        # scope, so op_org_scope() can hand the converged read helpers access.UNSCOPED
        # instead of a bare None (which now fails closed under the gate).
        args._op_unscoped = True
        return
    raise CliError(
        "multi-org mode is ON: an in-process command must be scoped. Pass --org <id> "
        "to scope to one org, or --operator (with env LOC_TMS_OPERATOR_KEY) for an "
        "unscoped cross-org run. Refusing to sweep every org silently.",
        EXIT_USAGE,
    )


def require_operator(args, emit) -> None:
    """The gate for CROSS-ORG operator commands that can't be scoped to one org
    (the `org` group + `user org` move — org management is inherently cross-org).

    Refuses --remote (in-process operator host only, like require_local), then:
      * gate OFF (single-tenant): a strict no-op — the command runs freely, so
        creating a 2nd org on-prem is allowed but inert (byte-identical to today);
      * gate ON (SaaS): REQUIRES --operator (env LOC_TMS_OPERATOR_KEY vs the stored
        operator_key_hash). --org is NOT accepted here: you cannot scope org
        management to a single org. Without --operator the command is REFUSED
        (exit 2) so a customer/cron on a shared host can never manage orgs.
    """
    require_local(args)
    from app.kernel import access

    if not access.multi_org_active():
        return
    if getattr(args, "operator", False):
        _verify_operator_key()
        return
    raise CliError(
        "multi-org mode is ON: org management is a cross-org operator action. Pass "
        "--operator (with env LOC_TMS_OPERATOR_KEY) to authorize it. (--org cannot "
        "scope org management to a single org.)",
        EXIT_USAGE,
    )


class _OpScope:
    """A minimal creator-like object carrying the in-process --org scope so the
    content.* service stamping (which reads creator.org_id) puts new rows in the
    scoped org. Not a real User — just an org_id carrier."""

    __slots__ = ("email", "org_id")

    def __init__(self, org_id):
        self.org_id = org_id
        self.email = "operator"


def op_creator(args):
    """Return a creator object stamping new rows with the resolved in-process --org
    scope, or None (no scoping: gate off, or --operator unscoped cross-org). Only
    meaningful after require_org_scope() ran; safe (None) otherwise."""
    oid = getattr(args, "_op_org_id", None)
    return _OpScope(oid) if oid is not None else None


def op_org_id(args):
    """The resolved in-process --org scope id (or None). For list-filtering the
    local command bodies when --org was given."""
    return getattr(args, "_op_org_id", None)


def op_org_scope(args):
    """op_org_id(), but expressed in the vocabulary the #73-converged read helpers
    speak (access.org_scope_blocked / org_scope_value).

    Returns `access.UNSCOPED` ONLY for the key-verified `--operator` door (the one
    place where "no org" legitimately means "every org" — require_org_scope set
    `_op_unscoped` after checking LOC_TMS_OPERATOR_KEY). Everything else returns the
    plain scope id, including a None that never went through the gate — which the
    converged helpers now treat as blocked while multi-org is active, and as today's
    no-op while it is off."""
    if getattr(args, "_op_unscoped", False):
        from app.kernel import access

        return access.UNSCOPED
    return getattr(args, "_op_org_id", None)


def _resolve_org_scope(org):
    """Validate a --org value: a positive int id of an existing Organization."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Organization

    try:
        oid = int(org)
    except (TypeError, ValueError) as e:
        raise CliError(
            f"--org must be an organization id (integer), got {org!r}.", EXIT_USAGE
        ) from e
    with Session(engine) as s:
        if s.get(Organization, oid) is None:
            raise CliError(f"no such organization: {oid}", EXIT_USAGE)
    return oid


def _verify_operator_key() -> None:
    """Verify env LOC_TMS_OPERATOR_KEY against the stored operator_key_hash
    (SHA-256 hex). Both must be present and match, else refuse (exit 2)."""
    import hashlib
    import hmac
    import os

    from app.kernel import settings

    supplied = os.environ.get("LOC_TMS_OPERATOR_KEY", "")
    stored = (settings.get("operator_key_hash") or "").strip()
    if not stored:
        raise CliError(
            "--operator was given but no operator key is configured on this host "
            "(set operator_key_hash via `config set`).",
            EXIT_USAGE,
        )
    if not supplied:
        raise CliError(
            "--operator requires the LOC_TMS_OPERATOR_KEY environment variable.", EXIT_USAGE
        )
    digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, stored):
        raise CliError("operator key does not match (LOC_TMS_OPERATOR_KEY).", EXIT_USAGE)
