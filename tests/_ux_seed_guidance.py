"""Offline UX seed: language-assets guidance (style guide, content type, language set).

Documented in: DESIGN.md. Application modules are imported inside the functions, after
fresh_fixture() has pointed the app at its disposable data directory.
"""


def seed_guidance(post, workflow):
    """Content type, language set and an approved style guide."""
    # language-assets-guidance fixtures (offline, no model calls)
    post("/content-types", {"name": "Product UI", "description": "Product interface copy"})
    post(
        "/languages",
        {
            "name": "Launch languages",
            "source_locale": "en-US",
            "target_locales": ["fr-FR", "de-DE"],
        },
    )
    guide = post(
        "/style-guides",
        {
            "name": "Atlas brand voice",
            "customer_id": "1",
            "body": "Use a clear, direct tone. Keep product names unchanged.\n"
            "Use sentence case for headings.",
        },
    )
    post(f"/style-guides/{guide}/approve", {})
    return {"guide": guide}
