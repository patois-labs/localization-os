# Releases

One file per release. 1.0.0 is the first open source release; 0.10.0 and 0.11.0 are kept as pre-open-source history.

| File | Audience | Content |
|---|---|---|
| `<version>.md` | Customers and evaluators | Plain-language release notes: highlights, what's new by area, security, changed or removed, upgrade notes for self-hosted installs, known limitations. No issue numbers, internal names or file paths. Also the body of the GitHub release. |

Rules:

- A release is an annotated tag `v<version>` on main. `pyproject.toml` `version` matches the tag; the running
  app reports `git describe` from the image build (`/app/RELEASE`, ADR 0007).
- Every claim is checked against the code or the merge record. No em dashes.
- A printable HTML copy of the notes can be attached to the GitHub release.
