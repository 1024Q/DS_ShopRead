// Server-side revocation for account sessions. Every session has an id that
// logout revokes. Revocations persist in .data/ui-revoked.json
// until the session would have expired anyway.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function createSessions(directory = process.env.ECMP_UI_DATA_DIR || path.join(process.cwd(), '.data'), now = Date.now) {
  const file = path.join(directory, 'ui-revoked.json');
  let cache = { mtime: -1, revoked: {} };

  function load() {
    let stat;
    try {
      stat = fs.statSync(file);
    } catch {
      cache = { mtime: 0, revoked: {} };
      return cache.revoked;
    }
    if (stat.mtimeMs !== cache.mtime) {
      try {
        const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
        cache = { mtime: stat.mtimeMs, revoked: parsed && typeof parsed === 'object' ? parsed : {} };
      } catch {
        // A damaged file must not lock everyone out; keep the last good copy.
        cache.mtime = stat.mtimeMs;
      }
    }
    return cache.revoked;
  }

  return {
    newId() {
      return crypto.randomBytes(16).toString('base64url');
    },
    isRevoked(id) {
      if (typeof id !== 'string' || !id) return false;
      const expires = load()[id];
      return typeof expires === 'number' && expires > now();
    },
    revoke(id, expires) {
      if (typeof id !== 'string' || !id) return;
      const current = now();
      const revoked = { ...load() };
      for (const [key, value] of Object.entries(revoked)) if (!(value > current)) delete revoked[key];
      revoked[id] = Number(expires) > current ? Number(expires) : current + 8 * 3600000;
      fs.mkdirSync(directory, { recursive: true });
      const temp = `${file}.${process.pid}.tmp`;
      fs.writeFileSync(temp, JSON.stringify(revoked), { mode: 0o600 });
      fs.renameSync(temp, file);
      cache = { mtime: fs.statSync(file).mtimeMs, revoked };
    },
  };
}

// Valid only when the cookie is unexpired and its session was not logged out.
function activeSession(saved, sessions, now = Date.now) {
  return (
    !!saved && typeof saved.sid === 'string' && !!saved.sid && saved.expires > now() && !sessions.isRevoked(saved.sid)
  );
}

module.exports = { ...createSessions(), createSessions, activeSession };
