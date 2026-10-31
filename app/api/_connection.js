import { cookies } from 'next/headers';
import codec from '../../lib/ui-crypto.cjs';
import sessions from '../../lib/ui-sessions.cjs';
import users from '../../lib/ui-users.cjs';
export const encrypt = codec.encrypt;
export const decrypt = codec.decrypt;
export const PEER_HEADER = 'x-ecmp-request-peer';

// Secure cookies only on HTTPS so plain-HTTP LAN logins still persist; ECMP_COOKIE_SECURE=true|false overrides.
export function secureCookie(request) {
  return (
    process.env.ECMP_COOKIE_SECURE === 'true' ||
    (process.env.ECMP_COOKIE_SECURE !== 'false' && new URL(request.url).protocol === 'https:')
  );
}

// The custom server always overwrites this header with the socket (or trusted proxy) address.
export function peerHeaders(request) {
  const peer = request.headers.get(PEER_HEADER);
  return peer ? { [PEER_HEADER]: peer } : {};
}

export async function activeNode(config) {
  try {
    const saved = decrypt((await cookies()).get('pdd_active_node')?.value);
    return saved.explicit === true && saved.url === config.url && Number.isSafeInteger(saved.id) && saved.id > 0
      ? saved.id
      : 0;
  } catch {
    return 0;
  }
}
export async function connection() {
  try {
    const user = await currentUser();
    if (user) return { url: process.env.ECMP_CENTER_INTERNAL_URL || 'http://127.0.0.1:8010', token: users.centerToken(), center: true, user };
  } catch {}
  return null;
}

export async function currentUser() {
  try {
    const saved = decrypt((await cookies()).get('ecmp_center')?.value);
    return sessions.activeSession(saved, sessions) ? users.session(saved) : null;
  } catch { return null; }
}

export function sameOrigin(request) {
  const origin = request.headers.get('origin');
  return (
    !origin ||
    origin === new URL(request.url).origin ||
    origin === `${new URL(request.url).protocol}//${request.headers.get('host')}`
  );
}
