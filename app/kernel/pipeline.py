"""``app.kernel.pipeline`` -- the NARROW "embed just the engine" surface (K1).

A REAL submodule (not an alias shim) that re-exports the segment -> TM -> MT -> stitch ->
export pipeline modules FROM ``app.services.*`` DIRECTLY -- never through ``app.kernel`` (M3),
so importing it does not install or lean on the alias finder. This is the importable unit that
makes ``verify_kernel_facade.py``'s narrowness bound REAL: ``import app.kernel.pipeline`` pulls
~58 ``app.*`` modules and, among the heavy optional deps, ONLY ``httpx`` (via ``providers``) --
NOT ``smtplib``/PyJWT/``mailer``/``oidc``/``databricks_export`` and no web framework.

An embedder wanting the whole platform imports the broad surface (``from app.kernel import
tm, translate, export, ...``); one wanting just the engine imports THIS module.

Being a real file, ``python -m app.kernel.pipeline`` works (the alias shims do not -- they have
no ``get_code``). ``app/kernel/`` may hold only direct-child ``.py`` modules like this one; a
subpackage would be denied by the finder (see the package docstring).

Documented in: docs/architecture/kernel-and-ports.md
"""

from app.services import (
    delivery,
    export,
    glossary,
    glossary_module,
    ingest,
    lqa,
    orchestrator,
    providers,
    quality,
    segment,
    segmentation_profiles,
    srx,
    tm,
    tm_lock,
    tm_module,
    translate,
)

__all__ = [
    "delivery",
    "export",
    "glossary",
    "glossary_module",
    "ingest",
    "lqa",
    "orchestrator",
    "providers",
    "quality",
    "segment",
    "segmentation_profiles",
    "srx",
    "tm",
    "tm_lock",
    "tm_module",
    "translate",
]
