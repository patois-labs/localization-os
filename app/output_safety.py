"""Output boundaries for untrusted text; no database or network dependencies."""

from urllib.parse import urlsplit


class InvalidOutputURL(ValueError):
    """A stored URL is not safe to expose as an external link."""


class _GuardedCSVText(str):
    """Remember an in-process guard until the final writer; serialized text is ordinary."""


def csv_safe_cell(value):
    """Keep spreadsheet applications from interpreting exported text as a formula."""
    if isinstance(value, _GuardedCSVText):
        return value
    if isinstance(value, str) and (
        value.startswith(("'", "\t", "\r", "\n"))
        or value.lstrip().startswith(("=", "+", "-", "@", "\uff1d", "\uff0b", "\uff0d", "\uff20"))
    ):
        return _GuardedCSVText("'" + value)
    return value


def csv_original_cell(value):
    """Strip exactly one export guard, including the guard on a literal apostrophe."""
    if isinstance(value, str) and value.startswith("'") and csv_safe_cell(value[1:]) != value[1:]:
        return value[1:]
    return value


def xlsx_text_cell(cell, value):
    """Preserve literal text, including leading equals, without changing its content."""
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def safe_http_url(value):
    """Return only absolute HTTP(S) URLs, or an empty string for unsafe stored values."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if any(ord(c) < 32 for c in value):
        return ""
    if not value.lower().startswith(("http://", "https://")):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.hostname and not any(c.isspace() for c in parsed.netloc):
            _port = parsed.port  # reject malformed ports as well as malformed hosts
            return value
    except ValueError:
        pass
    return ""
