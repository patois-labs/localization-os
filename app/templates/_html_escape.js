// Shared HTML text and quoted-attribute escaping for inline template scripts.
const ESCAPES = {'<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;'};
function esc(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/[<>&"']/g, (c) => ESCAPES[c]);
}
