// Never accept a browser-supplied forwarded address as the rate-limit identity.
// Behind a reverse proxy, list its address(es) in ECMP_TRUSTED_PROXIES (comma
// separated); only then is the right-most X-Forwarded-For entry used.
function trustedProxies(value = process.env.ECMP_TRUSTED_PROXIES || '') {
  return new Set(
    value
      .split(',')
      .map((s) => normalize(s.trim()))
      .filter(Boolean),
  );
}
function normalize(address) {
  return String(address || '').replace(/^::ffff:/, '');
}
function peerOf(req, trusted = trustedProxies()) {
  const socket = normalize(req.socket.remoteAddress) || 'unknown';
  if (!trusted.has(socket)) return socket;
  const forwarded = String(req.headers['x-forwarded-for'] || '')
    .split(',')
    .map((s) => normalize(s.trim()))
    .filter(Boolean);
  // Right-most entry is the one our trusted proxy appended; earlier entries are client-controlled.
  const candidate = forwarded.at(-1);
  return candidate && candidate.length <= 64 ? candidate : socket;
}
function stampPeer(req) {
  req.headers['x-ecmp-request-peer'] = peerOf(req);
}
module.exports = { stampPeer, peerOf, trustedProxies };
