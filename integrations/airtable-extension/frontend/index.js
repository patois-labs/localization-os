/**
 * Localization OS — Airtable Extension
 *
 * Adds a panel inside Airtable to trigger a Localization OS sync for a configured
 * connection. It does NOT translate anything itself — it calls Localization OS's
 * inbound API, which pulls this base's records, translates them, and pushes the
 * results back into your target field.
 *
 * Settings:
 *   - Server URL      kept per user in this browser (never in the shared GlobalConfig, which
 *                     any base editor can change). The user must confirm ("Trust this
 *                     server") before a key is ever sent, and must re-confirm if it changes.
 *   - connectionId    the Localization OS Airtable connection id (shared GlobalConfig)
 * The per-user API key is held in memory only, never in shared GlobalConfig.
 */
import {
  initializeBlock, useGlobalConfig, Box, Button, Heading, Text, Input,
  FormField, useBase, Loader,
} from '@airtable/blocks/ui';
import React, { useState } from 'react';
import { normalizeServerUrl, isTrustedOrigin } from './trust';

const URL_KEY = 'locStudioUrl';
const TRUST_KEY = 'loc-trusted-origin';

function readLocal(key) {
  try { return window.localStorage.getItem(key) || ''; } catch (e) { return ''; }
}
function writeLocal(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* private mode: not persisted */ }
}

function api(origin, token, path) {
  // Build from the parsed ORIGIN only, never the raw typed string (path, query and
  // fragment tricks cannot change where the key goes).
  return fetch(`${origin}/api/connectors${path}`, {
    method: 'POST',
    redirect: 'error', // never replay the key to a redirect target
    headers: { 'Content-Type': 'application/json', 'X-Loc-Token': token },
  }).then(async (r) => ({ ok: r.ok, body: await r.json().catch(() => ({})) }));
}

function LocStudioApp() {
  const globalConfig = useGlobalConfig();
  const base = useBase();
  const [url, setUrlState] = useState(readLocal(URL_KEY));
  const [trustedOrigin, setTrustedOrigin] = useState(readLocal(TRUST_KEY));
  // A URL another collaborator saved in the shared config is only a suggestion.
  const suggested = globalConfig.get('locStudioUrl') || '';
  const setUrl = (v) => { setUrlState(v); writeLocal(URL_KEY, v); };
  const norm = normalizeServerUrl(url);
  const trusted = isTrustedOrigin(url, trustedOrigin);
  const [token, setToken] = useState('');
  const connectionId = globalConfig.get('connectionId') || '';
  const canEdit = globalConfig.checkPermissionsForSet().hasPermission;

  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);

  const run = async (direction) => {
    if (!url || !token || !connectionId) {
      setStatus('⚠ Fill in Localization OS URL, API token, and connection id below.');
      return;
    }
    if (!trusted) {
      setStatus('⚠ Confirm the server below with "Trust this server" before sending your key.');
      return;
    }
    setBusy(true);
    setStatus(`Running ${direction}…`);
    try {
      const { ok, body } = await api(norm.origin, token, `/${connectionId}/${direction}`);
      setStatus((ok ? '✅ ' : '❌ ') + (body.message || JSON.stringify(body)));
    } catch (e) {
      setStatus(`❌ ${e.message} (is Localization OS reachable at ${url}?)`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box padding={3}>
      <Heading>Localization OS · {base.name}</Heading>
      <Text textColor="light" marginBottom={2}>
        Pull this base into Localization OS for translation, then push the translations back.
      </Text>

      <Box display="flex" alignItems="center" marginBottom={2}>
        <Button icon="play" variant="primary" disabled={busy} onClick={() => run('sync')} marginRight={2}>
          Translate with Localization OS
        </Button>
        {busy && <Loader scale={0.4} />}
      </Box>
      <Text textColor="light" size="small" marginBottom={3}>
        Sends this base to the TMS, translates it automatically, and writes the results back —
        you don't need to open Localization OS.
      </Text>
      {status ? <Text marginBottom={3}>{status}</Text> : null}

      <Box display="flex" marginBottom={3}>
        <Button size="small" disabled={busy} onClick={() => run('pull')} marginRight={2}>
          Pull only
        </Button>
        <Button size="small" disabled={busy} onClick={() => run('push')}>
          Push only
        </Button>
      </Box>

      <Heading size="small">Settings</Heading>
      <FormField label="Localization OS URL (saved for you only)">
        <Input value={url} onChange={(e) => setUrl(e.target.value)} />
      </FormField>
      {url && !norm.ok ? <Text textColor="red" size="small">{norm.error}</Text> : null}
      {suggested && suggested !== url ? (
        <Box marginBottom={2}>
          <Text size="small">A collaborator saved {suggested} in the shared settings. It is not
            used until you choose it and trust it yourself.</Text>
          <Button size="small" onClick={() => setUrl(suggested)}>Use this URL</Button>
        </Box>
      ) : null}
      {norm.ok && !trusted ? (
        <Box marginBottom={2}>
          <Text size="small">Your key will be sent to {norm.origin}. Only continue if this is
            your own Localization OS server.</Text>
          <Button size="small" variant="primary" onClick={() => {
            writeLocal(TRUST_KEY, norm.origin);
            setTrustedOrigin(norm.origin);
          }}>Trust this server</Button>
        </Box>
      ) : null}
      {trusted ? <Text size="small" textColor="light">Trusted server: {trustedOrigin}</Text> : null}
      <FormField label="Your per-user API key (this session only)">
        <Input value={token} type="password"
          onChange={(e) => setToken(e.target.value)} />
      </FormField>
      <Text size="small">
        Create a key in your Localization OS account. Shared settings are visible to all base
        collaborators and can be changed by editors, so the server URL is kept per user and your key
        is only sent to a server you confirmed. Remove any old apiToken setting and rotate that legacy key.
      </Text>
      <FormField label="Connection id">
        <Input value={String(connectionId)} disabled={!canEdit}
          onChange={(e) => globalConfig.setAsync('connectionId', e.target.value)} />
      </FormField>
    </Box>
  );
}

initializeBlock(() => <LocStudioApp />);
