# Dependency audit operator notes

The local command also audits all installed developer-environment distributions, including
pip, as a separate advisory-only pass. This records actual installed versions without resolving
or installing dependencies. A Python advisory scan does not inventory native libraries bundled
inside wheels. The #226 image-scanning follow-up must inventory those libraries and the venv's
pip, including libxml2, libxslt, xmlsec, OpenSSL, libpq, image codecs and PDFium. Validate that
coverage with a known vulnerable wheel fixture before claiming the image scan closes this gap.

Native libraries vendored inside wheels (auditwheel `*.libs/`) are inventoried by
`scripts/wheel_native_inventory.py` against the maintained `scripts/native_advisories.json`
(currently libxml2 and libxslt only). Run `python scripts/wheel_native_inventory.py WHEEL...`
over built or downloaded wheels; exit 1 means a bundled library is below its fixed version.
Exit 2 is fail closed: an unreadable wheel, an empty or invalid advisory file (every entry needs
a public source link), or no native library found at all (pass `--allow-empty` if expected).
A library that has advisories but no readable version is printed as `UNVERIFIED` for a manual check.
`tests/verify_native_wheel_scan.py` builds a vulnerable wheel (libxml2 2.9.10) offline, runs the
real scanner over it and fails unless the component and advisory are reported (an optional Trivy
run is attempted only when `trivy` and a local DB via TRIVY_CACHE_DIR exist, else it prints SKIP).

Covered: shared libraries (`.so`, `.dylib`, `.dll`) in wheels whose version is in the file name or
an embedded `name version` string, for the components listed in the advisory file. A bare soname
(`libxml2.so.2`) carries no release and is reported with an unknown version (UNVERIFIED), not flagged.
Not covered: libpq and libxmlsec1 (entries removed until their advisories can be confirmed),
libraries statically linked into extension modules (for example lxml's own wheels, which link
libxml2 into the module: a version string inside the module is not attributed), `.pyd` modules, OpenSSL,
image codecs and PDFium (no advisory entries yet), advisories published after the file was last
reviewed (it is curated by hand, not a live feed), and Trivy itself, which does not see vendored
wheel libraries. Each advisory has one `affected_below` threshold, so fixes backported to older release branches are
not modelled. Extend the advisory file when a new native dependency ships.

Where the scanner runs (#620):
- The local pregate (the merge gate while Actions minutes are unavailable) runs it as the
  "Native library scan" step right after "Dependency audit": `wheel_native_inventory.py
  --installed --advisories <clone>/scripts/native_advisories.json` over the site-packages of the
  gate's interpreter. It is offline (no download, no install) and BLOCKING like the dependency
  audit: a finding, a scanner error, an empty inventory or a missing verdict line is a FAIL.
  UNVERIFIED lines are listed for a manual look and do not block. This checks the libraries of
  the environment that runs the gate, so it is strongest on a Linux venv, which matches
  the production image; on a Windows PC it sees the Windows wheels.
- The dep-audit workflow runs `scripts/native_wheel_audit.py --profile <name>` in every matrix
  profile: it downloads that profile's hash-locked wheels (`pip download --no-deps
  --only-binary=:all: --require-hashes`, one package at a time, nothing resolved or built) and
  scans them. A package with no binary wheel for the platform (the ocr profile's sdist-only
  antlr4 pin) is printed as SKIPPED because it cannot vendor a library; every other download or
  hash error exits 2. Exit 1 is a known-vulnerable bundled library.
Both paths read the same `scripts/native_advisories.json`. The Secure Software Development
Policy makes no claim about native-wheel coverage, so its wording is unchanged.

The default-install CI job installs the reviewed hash lock with no dependency resolution,
runs pip check, and enforces copyleft scanning over every installed distribution. Local runs
also report optional/development license findings separately from the base-runtime verdict.

The local pregate is the merge gate even when Actions minutes are unavailable.
Every merge requires a completed advisory scan. Feed failures and timeouts block
the gate; there is no implicit offline waiver.

Both audit entry points read exact pins and call pip-audit with `--no-deps --disable-pip`.
They never install, resolve packages or execute package build backends. Requirement
options and direct URLs are rejected. SHA256 annotations in pinned files are accepted
as inventory data; this advisory-only scan does not download or verify artifacts.

Optional profiles listed in `.github/runtime-profiles.json` require a reviewed
`requirements-NAME.lock` containing their full transitive closure, constrained by
`requirements.lock`. The manifest is a declaration, never pip command arguments.
Missing, empty or unpinned inventories fail before contacting the advisory service.
The seven optional locks were generated and reviewed on 2026-09-28 (uv pip compile
--universal --python-version 3.13, constrained by requirements.lock and the reviewed dev
environment). They are closure inventories, not a wheel-only guarantee: the ocr lock pins
antlr4-python3-runtime 4.9.3, which ships only as an sdist, so installing the ocr profile
builds it locally (the gate itself never builds it). Do not use `--locked-only`
as merge evidence: it intentionally excludes optional inventories.

Generate these locks on a disposable, networked machine with no provider keys,
customer data or host directories mounted. Use wheel-only resolution, review the
entire closure and hashes, and commit the reviewed artifacts. If a package has no
wheel, replace it or arrange a separately reviewed build; do not allow local sdist
execution in the pregate. Never infer a lock from the direct package versions alone.
Run the full local pregate again after the locks are supplied.

The npm integration READMEs contain the required lock generation and release audit
commands. Their CI audit fails if a reviewed lock is missing or the registry cannot
be reached. Run those audits locally before integration releases when CI is unavailable.

All workflows declare read-only contents permission. As defense in depth, an operator
should also select read-only workflow permissions in repository Settings > Actions >
General. Repository settings cannot be inspected or changed by this offline train.
