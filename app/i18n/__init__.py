"""UI internationalization: JSON catalogs + a hand-rolled loader/translate().

English (en.json) is the source of truth and the fallback for every missing
key/locale, so English stays the compiled-in default.
"""

from app.i18n.loader import (
    available_locales,  # noqa: F401
    js_catalog,  # noqa: F401
    load_catalog,  # noqa: F401
    translate,  # noqa: F401
)
