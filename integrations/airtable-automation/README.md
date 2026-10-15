# Localization OS - Airtable Automation (self-service translation)

This is the lightest-weight "element in Airtable": a built-in **Automation** (no
extension to install) that, on a trigger, calls Localization OS. Localization OS reads the
table, **auto-detects your target-language columns by naming convention**, and
fills them in. End users just click a button (or set a status) and never need to
know a TMS exists.

## The naming convention
Pick one source column (e.g. **`Notes`**) and create one target column per
language using the pattern **`Notes (<locale>)`**:

| Notes (source) | Notes (es-MX) | Notes (de-DE) | Notes (fr-FR) |
|----------------|---------------|---------------|---------------|
| Hello world    | *(auto-filled)* | *(auto-filled)* | *(auto-filled)* |

Add or remove a language column anytime - no Localization OS change needed. (The
pattern is configurable per connection in Localization OS; default is `{src} ({loc})`.)

> Airtable can't trigger on "a column was created" - schema changes aren't
> automation triggers. So you add the column once, then **trigger a row** with a
> button or a status change; Localization OS fills every matching language column.

## One-time setup (an admin does this once)
1. In Localization OS: **Connectors → Airtable → add a connection** with your PAT and
   the **Source field** (e.g. `Notes`). Set **Default base ID** and **Default table**
   to the base and table this automation runs in: the webhook only accepts the
   connection's own base and table and refuses any other (one connection per
   base/table). Only an admin can change the base or table later (a linguist can
   only by entering their own token in the same save). Note the **connection id**. Create a per-user key in Account for a
   dedicated linguist account confined to this customer. Legacy shared tokens are refused.
   Automation inputs are readable by base collaborators with automation access. Only use
   this automation where those collaborators may exercise the account's permissions.
2. In Airtable: **Automations → Create automation**. Choose a trigger:
   - **When record matches conditions** → e.g. `Status` is `Ready for translation`, **or**
   - add a **Button field** and use **When button clicked**.
3. Add a **Run a script** action and paste [`translate-automation.js`](translate-automation.js).
4. In the script step's input panel, add these variables:
   - `locStudioUrl` = your Localization OS URL (tunnel URL if it runs on localhost)
   - `apiToken` = the dedicated account's per-user key
   - `connectionId` = the connection id
   - `sourceField` = `Notes`
   - `recordId` = the trigger's **Airtable record ID**
   - `tableName` = the trigger's table name
5. **Test** the step, then turn the automation on.

## What the user does
Set the row's `Status` to *Ready for translation* (or click the button). Within a
moment, every `Notes (xx-XX)` column on that row fills in. Done.

## How it reaches Localization OS
```
POST {locStudioUrl}/api/connectors/{connectionId}/airtable/webhook
header X-Loc-Token: <api token>
body   { base_id, table, record_ids: [recordId], source_field }
```
`base_id` and `table` must equal the connection's configured base and table (or be
omitted); anything else is answered with HTTP 400 and nothing is read or written.
Localization OS auto-detects the target columns, translates with the connection's
engine (TM- and glossary-aware), and writes each translation back to its column.
If your Localization OS runs on localhost, expose it with a tunnel first (see
`../README.md`).
