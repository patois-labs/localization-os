/**
 * Server-origin trust rules for the Airtable extension (issue #517).
 *
 * A user's API key must only ever go to an origin that THAT user typed and confirmed.
 * It must never go to a value that another base editor can change (GlobalConfig).
 * Pure functions, no Airtable imports, so they can be unit tested under plain Node.
 */

const LOCAL_HOSTS = ['localhost', '127.0.0.1', '[::1]'];

/** Returns {ok: true, origin} for an acceptable server URL, else {ok: false, error}. */
export function normalizeServerUrl(raw) {
  let u;
  try {
    u = new URL(String(raw || '').trim());
  } catch (e) {
    return { ok: false, error: 'Enter a full URL such as https://your-server.example.com' };
  }
  if (u.username || u.password) {
    return { ok: false, error: 'The URL must not contain a user name or password.' };
  }
  const local = LOCAL_HOSTS.includes(u.hostname);
  if (u.protocol !== 'https:' && !(u.protocol === 'http:' && local)) {
    return { ok: false, error: 'The server URL must start with https:// (http:// only for localhost).' };
  }
  return { ok: true, origin: u.origin };
}

/** True only when the typed URL is acceptable AND its origin is the one this user trusted. */
export function isTrustedOrigin(raw, trustedOrigin) {
  const n = normalizeServerUrl(raw);
  return Boolean(n.ok && trustedOrigin && n.origin === trustedOrigin);
}
