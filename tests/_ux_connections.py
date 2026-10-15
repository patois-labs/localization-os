"""DOM contract extraction for section 14, shared by its offline regression test."""

from tests._ux_golden import FormContractParser

PAGES = (
    "connectors",
    "connectors-catalog",
    "connectors-manage",
    "connector-type",
    "connection",
    "vendors",
    "vendor-detail",
)


_KEEP = (
    "name",
    "type",
    "value",
    "id",
    "required",
    "checked",
    "selected",
    "disabled",
    "readonly",
    "multiple",
    "min",
    "max",
    "step",
    "accept",
    "method",
    "action",
    "enctype",
    "cid",
    "placeholder",
)


class Contract(FormContractParser):
    """Capture form ownership, submitted attributes, hooks and navigation, not layout.

    <main> only; an allowlist of attributes plus every data-*; controls outside a form are
    recorded too."""

    main_only = True
    allow_nested_forms = True

    def __init__(self, html):
        self.links, self.cards, self.controls = [], [], []
        super().__init__(html)

    @staticmethod
    def _keep(a):
        return {k: v for k, v in a.items() if k in _KEEP or k.startswith("data-")}

    def scan(self, tag, a):
        if a.get("data-card"):
            self.cards.append(a["data-card"])
        if tag == "a" and a.get("href", "").startswith("/"):
            self.links.append(a["href"])

    def form_record(self, a):
        return {"attrs": self._keep(a), "controls": []}

    def control_attrs(self, a):
        keep = self._keep(a)
        if (
            "data-select-on-click" in a
            and "readonly" in a
            and not a.get("value", "").startswith("http")
        ):
            # The retired shared token is generated per disposable database.
            keep["value"] = "<generated legacy token>"
        return keep

    def record_control(self, tag, attrs):
        entry = [tag, attrs]
        if self.form is not None:
            self.form["controls"].append(entry)
        else:
            self.controls.append(entry)

    def snapshot(self):
        return {
            "forms": self.forms,
            "links": sorted(self.links),
            "cards": self.cards,
            "controls": self.controls,
        }
