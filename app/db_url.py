"""Credential-safe database URL presentation.

Documented in: docs/architecture/backups-and-dr.md
"""

import re


def redact_db_url(value: str) -> str:
    """Replace URL userinfo, including percent-encoded credentials, with ***.

    Takes a URL, NOT a composed message. The match is anchored on a ``scheme://`` and stays
    greedy WITHIN the authority, excluding only whitespace and ``/`` (the two characters that
    end a URL's authority) -- NOT ``@`` (MED-1 round 2: a password may legally contain a raw
    ``@``, e.g. ``postgresql://u:pa@ss@h/d``, which SQLAlchemy splits on the LAST ``@``;
    stopping at the FIRST ``@`` left the tail of the password in the clear). Because the class
    still excludes ``/`` and whitespace, the helper is safe on text that contains a URL plus
    other content -- it redacts only the credentials inside the URL token and never swallows a
    later ``@`` on the line (e.g. an email address). Percent-encoded credentials (``%40``,
    ``%2F``) are still redacted."""
    return re.sub(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/]*@", r"\1***@", str(value))
