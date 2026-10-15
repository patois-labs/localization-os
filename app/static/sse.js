// Generic SSE reconnect-with-backoff helper. Opens a marked fetch stream to
// `url`, invoking opts.onMessage(ev) for every message. On a dropped
// connection, closes and retries with capped backoff, re-attaching the same
// handlers each time. After MAX_RETRIES failed attempts, gives up and calls
// opts.onGiveUp() instead of failing silently. Returns a handle with
// `close()` so the caller can shut the connection down cleanly (e.g. on a
// server-sent 'done' event) without triggering a reconnect.
function connectSSE(url, opts) {
  opts = opts || {};
  const onMessage = opts.onMessage || function () {};
  const onGiveUp = opts.onGiveUp || function () {};

  const MAX_RETRIES = 5;
  let es = null;
  let attempt = 0;
  let gaveUp = false;

  function closeForGood() {
    gaveUp = true;
    if (es) es.close();
  }

  async function connect() {
    if (gaveUp) return;
    const controller = new AbortController();
    es = { close: function () { controller.abort(); } };
    try {
      // Native EventSource cannot carry the background marker header.
      const response = await fetch(url, {
        credentials: 'same-origin', signal: controller.signal,
        headers: { Accept: 'text/event-stream', 'X-Loc-Background': '1' }
      });
      const contentType = response.headers.get('content-type') || '';
      if (!response.ok || !contentType.includes('text/event-stream')) {
        throw new Error('Stream unavailable');
      }
      if (opts.onConnect) opts.onConnect();
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = '', data = [];
      while (!gaveUp) {
        const chunk = await reader.read();
        if (chunk.done) break;
        pending += decoder.decode(chunk.value, { stream: true });
        let end;
        while (!gaveUp && (end = pending.indexOf('\n')) !== -1) {
          const line = pending.slice(0, end).replace(/\r$/, '');
          pending = pending.slice(end + 1);
          if (line === '') {
            if (data.length) {
              // A healthy event proves the stream works: the retry budget counts
              // CONSECUTIVE failures, so proxy idle timeouts over an hour never add up (#545).
              attempt = 0;
              onMessage({ data: data.join('\n') });
            }
            data = [];
          } else if (line.startsWith('data:')) {
            data.push(line.slice(5).replace(/^ /, ''));
          }
        }
      }
    } catch (_) { /* Network errors use the same bounded reconnect path as EOF. */ }
    finally {
      controller.abort();
      if (gaveUp) return;
      attempt += 1;
      if (attempt > MAX_RETRIES) {
        onGiveUp();
        return;
      }
      const delay = Math.min(1000 * Math.pow(2, attempt - 1), 15000);
      setTimeout(connect, delay);
    }
  }
  connect();

  return { close: closeForGood };
}
