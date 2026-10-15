"""Reproduce the streamable-http request-body sizing behind the MCP body cap.

The MCP server caps streamable-http POST bodies at
`integrations/mcp-server/server.py:_MAX_REQUEST_BODY_SIZE` (8 MiB). The cap comment
cites wire sizes for the largest INLINE bulk imports; this helper is where those
numbers come from, so they stay reproducible and honest about the ASSUMPTION that
drives them -- key/value SHAPE. (Adapted from the Path A adversarial reviewer's
probe_size.py, 2026-08-06.)

Over remote HTTP there is no shared filesystem, so a bulk import rides inline in a
tool argument (import_keys `data`, manage_tm TMX `data`). The body the cap measures
is the whole JSON-RPC tools/call envelope, and its size is dominated by that inline
string. Run:

    .venv\\Scripts\\python.exe scripts\\mcp_body_size.py

No app, no network, no dependencies beyond the stdlib -- pure arithmetic on the
wire envelope, so it is safe to run anywhere.
"""

import json

MIB = 1024 * 1024


def envelope(tool: str, args: dict) -> bytes:
    """The exact JSON-RPC tools/call POST body a client sends (compact, as a real
    client serialises it)."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _mib(b: bytes) -> float:
    return len(b) / MIB


#: The three key/value shapes the cap comment names. THIS is the assumption the
#: numbers depend on: a 30,000-key catalogue is ~1.3 MiB at the short shape and
#: ~4.3 MiB at the long shape - a >3x spread - so any single "N keys -> X MiB"
#: claim is only meaningful WITH its shape stated.
_KEY_SHAPES = (
    (
        "short  (~20-char key / ~25-char value)",
        lambda i: "app.s%d.lbl" % i,  # noqa: UP031
        lambda i: "Save changes now",
    ),
    (
        "medium (~38-char key / ~48-char value)",
        lambda i: "app.settings.section%d.label_field_%d" % (i % 50, i),  # noqa: UP031
        lambda i: "Your changes have been saved successfully.",
    ),
    (
        "long   (~55-char key / ~90-char value)",
        lambda i: "com.acme.product.module%d.screen.component.label_id_%06d" % (i % 30, i),  # noqa: UP031
        lambda i: "We could not complete that action right now. Please try again in a few moments.",
    ),
)

_TU = (
    '  <tu>\n    <tuv xml:lang="en"><seg>%s</seg></tuv>\n'
    '    <tuv xml:lang="fr"><seg>%s</seg></tuv>\n  </tu>\n'
)
_TMX_SHAPES = (
    (
        "short seg (~30 chars)",
        "Your changes have been saved.",
        "Vos modifications ont ete enregistrees.",
    ),
    (
        "medium seg (~80 chars)",
        "We could not complete that action right now. Please try again in a moment.",
        (
            "Nous n'avons pas pu effectuer cette action pour le moment. Veuillez "
            "reessayer dans un instant."
        ),
    ),
)


def _import_keys_body(n: int, kf, vf) -> bytes:
    # A realistic exported catalogue: pretty-printed JSON object, one key per line.
    cat = {kf(i): vf(i) for i in range(n)}
    data = json.dumps(cat, ensure_ascii=False, indent=2)
    return envelope("import_keys", {"project_id": 7, "data": data})


def _manage_tm_body(n: int, src: str, tgt: str) -> bytes:
    tmx = (
        '<?xml version="1.0"?>\n<tmx version="1.4"><header/><body>\n'
        + "".join(_TU % (src, tgt) for _ in range(n))
        + "</body></tmx>"
    )
    return envelope("manage_tm", {"action": "import", "data": tmx})


def main() -> None:
    cap = 8 * MIB
    print(
        "MCP streamable-http body sizing (cap = 8 MiB). Sizes are the full "
        "JSON-RPC tools/call wire envelope.\n"
    )

    print("import_keys inline `data` (flat JSON catalogue):")
    for n in (20000, 30000):
        for label, kf, vf in _KEY_SHAPES:
            body = _import_keys_body(n, kf, vf)
            print(
                "  %6d keys  %-42s -> %5.2f MiB  (%3.0f B/key)"  # noqa: UP031
                % (n, label, _mib(body), len(body) / n)
            )

    print("\nmanage_tm inline TMX `data`:")
    for n in (20000,):
        for label, src, tgt in _TMX_SHAPES:
            body = _manage_tm_body(n, src, tgt)
            print(
                "  %6d units %-42s -> %5.2f MiB  (%3.0f B/unit)"  # noqa: UP031
                % (n, label, _mib(body), len(body) / n)
            )

    # Capacity of the 8 MiB cap at the MEDIUM key shape, and the largest legitimate
    # body we measure (a 20k-unit TMX at medium segment length).
    per_key = len(_import_keys_body(20000, _KEY_SHAPES[1][1], _KEY_SHAPES[1][2])) / 20000
    largest = max(_mib(_manage_tm_body(20000, s, t)) for _l, s, t in _TMX_SHAPES)
    print(
        "\nAt the medium key shape (%0.0f B/key): 8 MiB fits ~%d keys; 4 MiB "  # noqa: UP031
        "(SDK default) fits ~%d." % (per_key, cap / per_key, 4 * MIB / per_key)
    )
    print(
        f"Largest legitimate body measured: ~{largest:.1f} MiB -> 8 MiB leaves ~{(cap / MIB) / largest:.1f}x headroom."  # noqa: E501
    )


if __name__ == "__main__":
    main()
