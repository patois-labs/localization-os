"""Shared plumbing for the UX form-contract goldens (#553).

Four suites used to carry their own HTMLParser that walks a rendered page and records forms and
controls (verify_ux_admin_insight, verify_ux_admin_settings, verify_ux_guidance and
_ux_connections). They differ only in WHAT they keep (an attribute allowlist or every attribute,
the whole page or only <main>, controls inside forms only or everywhere), so each is now a small
subclass of `FormContractParser` that sets those knobs and nothing else. Every golden file is
byte-identical to before: the strictness of each suite did not change.

`write_requested` and `write_golden` are the shared "regenerate after an intended markup change"
plumbing: set the suite's *_WRITE=1 variable, run the suite, review the diff of the fixture.
"""

import os
from html.parser import HTMLParser

CONTROL_TAGS = ("input", "select", "textarea", "option", "button")


class FormContractParser(HTMLParser):
    """Walk a page and record its forms and controls.

    Subclass hooks (all optional):
      main_only          only look inside <main> (shell chrome is repeated on every page)
      allow_nested_forms False asserts a <form> never opens inside another
      form_record(a)     the record to store for a <form> with attributes `a`
      open_form(a)       store the record, return what `self.form` should hold while it is open
      control_attrs(a)   the attributes to keep for a control (default: all of them)
      record_control(tag, attrs)  where a control goes (default: nowhere)
      scan(tag, a)       see every tag the parser looks at (ids, counters, links)
    A csrf_token control never keeps its value: it differs on every render.
    """

    main_only = False
    allow_nested_forms = False

    def __init__(self, html=None):
        super().__init__()
        self.forms = []
        self.form = None
        self.ids = []
        self.in_main = not self.main_only
        if html is not None:
            self.feed(html)

    def form_record(self, a):
        return {"attributes": dict(a), "controls": []}

    def open_form(self, a):
        record = self.form_record(a)
        self.forms.append(record)
        return record

    def control_attrs(self, a):
        return dict(a)

    def record_control(self, tag, attrs):
        raise NotImplementedError

    def scan(self, tag, a):
        if a.get("id"):
            self.ids.append(a["id"])

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "main":
            self.in_main = True
        if not self.in_main:
            return
        self.scan(tag, a)
        if tag == "form":
            if not self.allow_nested_forms:
                assert self.form is None, "Nested form"
            self.form = self.open_form(a)
        elif tag in CONTROL_TAGS:
            kept = self.control_attrs(a)
            if a.get("name") == "csrf_token":
                kept.pop("value", None)
            self.record_control(tag, kept)

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None
        if tag == "main" and self.main_only:
            self.in_main = False


def write_requested(env_var):
    """True when the suite was run with `<env_var>=1`: rewrite the golden instead of reading it."""
    return os.environ.get(env_var) == "1"


def write_golden(path, text):
    """Write a golden text file with LF line endings, whatever the platform."""
    path.write_bytes(text.encode("utf-8"))
