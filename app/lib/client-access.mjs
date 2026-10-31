import permissions from '../../lib/ui-permissions.cjs';
let user;
export function setClientAccess(value) {
  user = value;
}
// UI convenience only: every request is independently authorized on the server.
export function canCollect(kind) {
  return (
    user === undefined || permissions.can(user, kind === 'reconciliation_month' ? 'reconciliation_day' : kind, true)
  );
}
