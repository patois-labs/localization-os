"""UI i18n catalog loader + translate().

Hand-rolled JSON catalogs (no Babel/gettext): each app/i18n/<locale>.json holds
nested-namespace keys (nav.*, page.*, chrome.*, role.*, js.*). English is the
compiled-in source of truth and the fallback for every missing key/locale, so
the whole existing test suite (which never sets a locale) keeps rendering the
exact English strings it pins.

Resolution order for a key:  <locale> value  ->  en value  ->  the key itself.
Placeholders are `{name}` tokens filled with str.format(**kwargs); a format
failure (bad/missing token) degrades to the un-formatted string rather than
raising in a template.
"""

import json
import threading
from contextlib import suppress
from pathlib import Path

from markupsafe import Markup, escape

DEFAULT_LOCALE = "en"
_DIR = Path(__file__).resolve().parent

# Read-through cache, same shape as settings._cache: catalogs are small and
# immutable at runtime, so we load each locale's JSON once per process.
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def available_locales() -> list[str]:
    """Locale codes that have a catalog file, en first then the rest sorted."""
    codes = sorted(p.stem for p in _DIR.glob("*.json"))
    if DEFAULT_LOCALE in codes:
        codes.remove(DEFAULT_LOCALE)
        codes.insert(0, DEFAULT_LOCALE)
    return codes


def _flatten(d: dict, prefix: str = "") -> dict:
    """nav.dashboard style flat map from a nested dict."""
    out: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def load_catalog(locale: str) -> dict:
    """Flat {dotted.key: value} catalog for a locale (cached). Unknown locale ->
    empty dict (callers fall back to en). Never raises on a missing/bad file."""
    locale = (locale or "").strip() or DEFAULT_LOCALE
    with _cache_lock:
        if locale in _cache:
            return _cache[locale]
    path = _DIR / f"{locale}.json"
    if not path.exists():
        catalog: dict = {}
    else:
        try:
            catalog = _flatten(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            catalog = {}
    with _cache_lock:
        _cache[locale] = catalog
    return catalog


def _normalize(locale: str) -> str:
    """An unknown locale collapses to en; a known one passes through."""
    locale = (locale or "").strip() or DEFAULT_LOCALE
    return locale if locale in available_locales() else DEFAULT_LOCALE


def translate(locale: str, key: str, **kwargs) -> str:
    """Look up `key` in `locale`, falling back to en, then to the key itself.

    `{placeholder}` tokens in the value are filled from **kwargs. A KeyError or
    other format failure returns the raw (un-formatted) value so a template never
    500s on a bad token."""
    locale = _normalize(locale)
    value = load_catalog(locale).get(key)
    if value is None and locale != DEFAULT_LOCALE:
        value = load_catalog(DEFAULT_LOCALE).get(key)
    if value is None:
        value = key
    # Only catalog keys ending in _html trust catalog markup. Escape interpolated
    # values first; ordinary keys stay plain strings for Jinja's normal autoescape.
    html = key.endswith("_html")
    if html:
        kwargs = {name: escape(str(arg)) for name, arg in kwargs.items()}
    if kwargs and isinstance(value, str) and "{" in value:
        with suppress(KeyError, IndexError, ValueError):
            value = value.format(**kwargs)
    return Markup(value) if html else value  # noqa: S704 -- trusted catalog *_html key; args escaped above


def resolved_ui_locale(locale: str) -> str:
    """a11y T11: the locale code a lang attribute may carry (a known catalog, else en)."""
    return _normalize(locale)


def resolved_locale(locale: str, key: str) -> str:
    """a11y T11 (WCAG 3.1.2): the language `translate(locale, key)` actually returns,
    so markup can declare it: the locale when its catalog has the key, else en (the
    fallback, and also the answer for a key no catalog has)."""
    locale = _normalize(locale)
    if locale != DEFAULT_LOCALE and load_catalog(locale).get(key) is not None:
        return locale
    return DEFAULT_LOCALE


def js_catalog(locale: str) -> dict:
    """The `js.*` slice for the current locale, merged over en, as a flat dict of
    short keys (no `js.` prefix) for injection into window.I18N."""
    locale = _normalize(locale)
    merged = {k: v for k, v in load_catalog(DEFAULT_LOCALE).items() if k.startswith("js.")}
    if locale != DEFAULT_LOCALE:
        merged.update({k: v for k, v in load_catalog(locale).items() if k.startswith("js.")})
    return {k[len("js.") :]: v for k, v in merged.items()}
