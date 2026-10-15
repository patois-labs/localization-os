# RUNBOOK - WidgetCMS connector live QA

Needs live-tenant QA. Hermetic tests (`tests/verify_widgetcms.py`) prove the seam logic offline; they
CANNOT prove real OAuth/token scopes, the exact live response shapes, or that a write
actually lands in WidgetCMS. This runbook stands up a throwaway WidgetCMS, runs the
end-to-end loop, and tears it down. No real customer data is touched.

Provenance: bb-io/WidgetCMS (MIT) — see THIRD_PARTY_NOTICES.md.

Do NOT run this unless the maintainer has said go IN THE CURRENT SESSION, and a live provider key
is configured for the sync project (else the mock-egress guard blocks the write — by design).

## 0. Cost / prerequisites

- TODO: sandbox tier + cost, account/plan needed, any paid feature gates.
- A live MT provider key on the Localization OS project the connection syncs into.

## 1. Sandbox setup

1. TODO: stand up a throwaway WidgetCMS instance / test account (numbered manual steps).
2. TODO: enable localization + create a source item with translatable fields.
3. TODO: create an API token / credential with the minimum scope.

## 2. Create the Localization OS connection

Web (Connectors -> Add -> WidgetCMS) or CLI:

```
.venv\Scripts\python.exe -m app.cli connector create --kind widgetcms --name "WidgetCMS QA" ^
  --config ... ^
  --secret ...
```

## 3. Run the translate loop

```
.venv\Scripts\python.exe -m app.cli connector widgetcms-translate <id> --locales "fr, de"
```

Expected: created on the first run; second run over unchanged source reports SKIPPED
(content-hash). Edit a source field, re-run: only that item re-translates.

## 4. MANDATORY live checks (blueprint §4 — the assumptions the design RESTS on)

Each MANDATORY item names an assumption the hermetic fake could only GUESS, and the revert
plan if the live box disproves it. Replace these placeholders with the connector's real ones.

### MANDATORY-1 — is the platform's update a partial merge or a full replace? (taxonomy B)

TODO: prove on a real box whether an UPDATE that sends only the changed fields KEEPS the
fields it omits. Revert plan if it is a REPLACE: read-current -> merge -> write-full.

### MANDATORY-2 — does the existence probe read the same version/state the write writes? (taxonomy C)

TODO: confirm the second run over unchanged source is SKIPPED, not re-created. Revert plan:
align the read/probe status/version param with the write target.

## 5. Teardown checklist

- [ ] Delete the Localization OS connection: `connector delete <id>`.
- [ ] Revoke the WidgetCMS API token / credential.
- [ ] Tear down the sandbox instance / account.
- [ ] Note any residue left in a box you keep, in the session log.

Nothing here touches the real Localization OS DB, `.env`, or any customer tenant.
