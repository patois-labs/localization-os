"""Request-scoped PII protection at AI transport boundaries.

Documented in: docs/architecture/engines-and-providers.md
"""

import functools
import inspect
import json
import re
from contextvars import ContextVar

_force_redaction = ContextVar("pii_force_redaction", default=False)


def call_with_redaction(force, fn, /, *args, **kwargs):
    """Carry a step's privacy choice through a synchronous call and its retries.

    Enter on the worker that makes the call, never across a generator yield.
    Nested calls cannot disable an outer scope; exceptions always reset it.
    """
    token = _force_redaction.set(bool(force) or _force_redaction.get())
    try:
        return fn(*args, **kwargs)
    finally:
        _force_redaction.reset(token)


_TOKEN = re.compile(r"«PII_[A-Z_]+_\d+»")


def _redaction_on() -> bool:
    """pii.should_redact(), safe at the engine boundary.

    An uninitialized database (no settings table: engine probes, bare unit harnesses)
    cannot have the switch on, so that reads as OFF. Any other failure to read the
    setting fails CLOSED (redact), like the ai_optout guard."""
    from app.services import pii

    if _force_redaction.get():
        return True
    try:
        return pii.should_redact()
    except Exception as exc:
        # No settings table -> OFF; every other failure -> redact.
        return "no such table" not in str(exc).lower()


def _map_strings(value, transform):
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, dict):
        return {
            _map_strings(key, transform): _map_strings(item, transform)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_map_strings(item, transform) for item in value]
    if isinstance(value, tuple):
        return tuple(_map_strings(item, transform) for item in value)
    return value


class RequestRedaction:
    """One mapping for all messages and payloads; never reuse a caller's token."""

    def __init__(self, value):
        self.active = _redaction_on()
        self.mapping = {}
        self.tokens_by_value = {}
        self.reserved = set()
        self.counter = 0
        if self.active:
            _map_strings(value, self._reserve)

    def _reserve(self, text):
        self.reserved.update(_TOKEN.findall(text))
        return text

    def _text(self, text):
        from app.services import pii

        def scrub(piece):
            masked, mapping = pii.redact(piece)
            replacements = {}
            for old, original in mapping.items():
                if original in self.tokens_by_value:
                    replacements[old] = self.tokens_by_value[original]
                    continue
                typ = old.rsplit("_", 1)[0]
                while True:
                    self.counter += 1
                    token = f"{typ}_{self.counter}»"
                    if token not in self.reserved:
                        break
                self.reserved.add(token)
                self.mapping[token] = original
                self.tokens_by_value[original] = token
                replacements[old] = token
            return _TOKEN.sub(lambda m: replacements.get(m[0], m[0]), masked)

        # Existing classic-path placeholders are opaque, including to optional NER.
        parts = []
        end = 0
        for match in _TOKEN.finditer(text):
            parts.extend((scrub(text[end : match.start()]), match[0]))
            end = match.end()
        parts.append(scrub(text[end:]))
        return "".join(parts)

    def redact(self, value):
        return _map_strings(value, self._text) if self.active else value

    def restore(self, value):
        def plain(text):
            return _TOKEN.sub(lambda m: self.mapping.get(m[0], m[0]), text)

        def restore_text(text):
            # Restore JSON as data so even a phone spanning a newline, or a NER
            # entity containing quotes, cannot turn a valid model answer into bad JSON.
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                restored = _map_strings(parsed, plain)
                return json.dumps(restored, ensure_ascii=False) if restored != parsed else text
            # JSON-producing tasks may escape the guillemets. Escape original values
            # as JSON string content in that case so quotes cannot corrupt the answer.
            for token, original in self.mapping.items():
                escaped = json.dumps(token)[1:-1]
                text = text.replace(escaped, json.dumps(original)[1:-1])
            return plain(text)

        return _map_strings(value, restore_text) if self.mapping else value


def wrap_callable(fn, fields):
    """Protect selected content arguments, preserving the exact flag-off call."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def protected(*args, **kwargs):
        if not _redaction_on():
            return fn(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        content = {key: value for key, value in bound.arguments.items() if key in fields}
        extra_name = next(
            (name for name, p in signature.parameters.items() if p.kind == p.VAR_KEYWORD),
            None,
        )
        extra = bound.arguments.get(extra_name, {})
        extra_content = {key: value for key, value in extra.items() if key in fields}
        content.update(extra_content)
        redaction = RequestRedaction(content)
        # Argument names are Python binding metadata, not model-facing content.
        masked = {key: redaction.redact(value) for key, value in content.items()}
        for key in content:
            if key not in extra_content:
                bound.arguments[key] = masked[key]
        if extra_content:
            bound.arguments[extra_name] = {**extra, **{key: masked[key] for key in extra_content}}
        return redaction.restore(fn(*bound.args, **bound.kwargs))

    return protected
