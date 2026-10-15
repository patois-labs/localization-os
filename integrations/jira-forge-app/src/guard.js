/**
 * Permission guards for the Forge resolver (issue #517).
 *
 * The resolver calls Localization OS with ONE install-wide key, so Localization OS cannot
 * tell which Jira user pressed a button. These guards ask Jira, as the invoking user,
 * whether that user may do the thing. Pure functions with the Jira caller injected so they
 * can be unit tested under plain Node. askJira({permissions, issueKey?}) returns the
 * response of GET /rest/api/3/mypermissions made as the invoking user. Everything fails closed: any error or an unexpected
 * answer is a refusal.
 */

async function myPermissions(askJira, params) {
  try {
    const res = await askJira(params);
    if (!res || !res.ok) return null;
    const body = await res.json();
    return (body && body.permissions) || null;
  } catch (e) {
    return null;
  }
}

/** True only for a Jira site administrator (global ADMINISTER permission). */
export async function isJiraAdmin(askJira) {
  const perms = await myPermissions(askJira, { permissions: 'ADMINISTER' });
  return Boolean(perms && perms.ADMINISTER && perms.ADMINISTER.havePermission === true);
}

/** True only when the invoking user can browse, edit and comment on this issue. */
export async function canTranslateIssue(askJira, issueKey) {
  if (!issueKey || !/^[A-Za-z][A-Za-z0-9_]*-\d+$/.test(issueKey)) return false;
  const want = ['BROWSE_PROJECTS', 'EDIT_ISSUES', 'ADD_COMMENTS'];
  const perms = await myPermissions(
    askJira,
    { issueKey, permissions: want.join(',') },
  );
  return Boolean(perms && want.every((p) => perms[p] && perms[p].havePermission === true));
}
