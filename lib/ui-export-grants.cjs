const { can, firstPage } = require('./ui-permissions.cjs');
function valid(saved, user, id, scope) {
  return (
    saved?.uid === user.id &&
    saved.version === user.version &&
    saved.jobs?.some((j) => j.id === id && j.scope === scope && j.expires > Date.now() && can(user, j.page))
  );
}
function add(saved, user, id, page, scope) {
  const jobs = saved?.uid === user.id && saved.version === user.version ? saved.jobs || [] : [];
  return {
    uid: user.id,
    version: user.version,
    jobs: [
      ...jobs.filter((j) => j.id !== id && j.expires > Date.now()).slice(-11),
      { id, page, scope, expires: Date.now() + 86400000 },
    ],
  };
}
function tablePage(user, body) {
  if (body.source === 'orders') return can(user, 'report_view') ? 'report_view' : null;
  // Inline exports contain only rows supplied by this browser, never a server data query.
  return firstPage(user) || null;
}
module.exports = { valid, add, tablePage };
