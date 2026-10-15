/**
 * Forge backend resolver. Calls the Localization OS inbound API from Forge's backend
 * (so the token never reaches the browser). Configure these as Forge environment
 * variables (see README):
 *   LOC_STUDIO_URL            e.g. https://your-loc-studio-host.example.com
 *   LOC_STUDIO_TOKEN          the Connector API token from Localization OS
 *   LOC_STUDIO_CONNECTION_ID  the Localization OS Jira connection id
 */
import Resolver from '@forge/resolver';
import api, { fetch, route } from '@forge/api';
import { isJiraAdmin, canTranslateIssue } from './guard';

const resolver = new Resolver();

const URL = process.env.LOC_STUDIO_URL;
const TOKEN = process.env.LOC_STUDIO_TOKEN;
const CONNECTION_ID = process.env.LOC_STUDIO_CONNECTION_ID;

// Comma-separated languages to translate into, e.g. "es-MX,de-DE". Optional —
// if unset, Localization OS uses the connection's configured target locales.
const TARGET_LOCALES = process.env.LOC_STUDIO_TARGET_LOCALES;

async function call(path, payload) {
  if (!URL || !TOKEN || !CONNECTION_ID) {
    return { ok: false, message: 'Not configured. Set LOC_STUDIO_URL / _TOKEN / _CONNECTION_ID.' };
  }
  try {
    const res = await fetch(`${URL.replace(/\/$/, '')}/api/connectors/${CONNECTION_ID}/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Loc-Token': TOKEN },
      body: payload ? JSON.stringify(payload) : undefined,
    });
    const body = await res.json().catch(() => ({}));
    return { ok: res.ok, message: body.message || `HTTP ${res.status}` };
  } catch (e) {
    return { ok: false, message: String(e) };
  }
}

// Ask Jira AS THE INVOKING USER (not as the app) so the answer reflects that user's rights.
const askJira = ({ permissions, issueKey }) => (issueKey
  ? api.asUser().requestJira(route`/rest/api/3/mypermissions?permissions=${permissions}&issueKey=${issueKey}`)
  : api.asUser().requestJira(route`/rest/api/3/mypermissions?permissions=${permissions}`));
const DENIED = { ok: false, message: 'Not allowed: your Jira permissions do not cover this action.' };

// Translate THE CURRENT issue on demand — posts a labelled comment per language.
resolver.define('translateThisIssue', async ({ context }) => {
  const key = context?.extension?.issue?.key;
  if (!key) return { ok: false, message: 'No issue in context.' };
  if (!(await canTranslateIssue(askJira, key))) return DENIED;
  const targets = TARGET_LOCALES ? TARGET_LOCALES.split(',').map((s) => s.trim()) : undefined;
  return call('jira/translate', { issue_key: key, target_locales: targets });
});

// The saved-JQL batch is paid and org-wide: Jira site admins only.
resolver.define('sync', async () => ((await isJiraAdmin(askJira)) ? call('sync') : DENIED));
resolver.define('config', () => ({ configured: Boolean(URL && TOKEN && CONNECTION_ID) }));

export const handler = resolver.getDefinitions();
