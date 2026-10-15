/**
 * Forge UI Kit issue panel. Renders two buttons that invoke the backend resolver,
 * which calls Localization OS. Keeps the token server-side.
 */
import React, { useEffect, useState } from 'react';
import ForgeReconciler, { Text, Button, ButtonGroup, Stack, Heading } from '@forge/react';
import { invoke } from '@forge/bridge';

const Panel = () => {
  const [cfg, setCfg] = useState(null);
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => { invoke('config').then(setCfg); }, []);

  const run = async (direction) => {
    setBusy(true);
    setStatus(`Running ${direction}…`);
    const r = await invoke(direction);
    setStatus((r.ok ? '✅ ' : '❌ ') + r.message);
    setBusy(false);
  };

  return (
    <Stack space="space.100">
      <Heading as="h3">Localization OS</Heading>
      <Text>Translate this issue (summary + description) into your target languages — each is
        posted back as a labelled comment, without leaving Jira.</Text>
      {cfg && !cfg.configured && (
        <Text>⚠ Not configured — set LOC_STUDIO_URL / _TOKEN / _CONNECTION_ID via `forge variables set`.</Text>
      )}
      <ButtonGroup>
        <Button text="Translate this issue" appearance="primary" isDisabled={busy} onClick={() => run('translateThisIssue')} />
        <Button text="Run saved JQL batch" isDisabled={busy} onClick={() => run('sync')} />
      </ButtonGroup>
      {status ? <Text>{status}</Text> : null}
    </Stack>
  );
};

ForgeReconciler.render(
  <React.StrictMode>
    <Panel />
  </React.StrictMode>
);
