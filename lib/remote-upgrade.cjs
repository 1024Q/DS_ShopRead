// Same-origin WebSocket tunnel. API credentials stay on the server.
const { decrypt } = require('./ui-crypto.cjs');
const sessions = require('./ui-sessions.cjs');
const http = require('node:http'),
  https = require('node:https');
function destination(req) {
  const host = req.headers.host,
    origin = new URL(req.headers.origin || 'invalid');
  if (origin.host !== host || !['http:', 'https:'].includes(origin.protocol)) throw Error('origin');
  const url = new URL(req.url, 'http://localhost');
  const match = url.pathname.match(/^\/browser-control\/(?:nodes\/(\d+)\/)?shops\/(\d+)$/);
  if (!match || url.search) throw Error('path');
  const cookies = Object.fromEntries(
    (req.headers.cookie || '').split(';').map((s) => s.trim().split(/=(.*)/s).slice(0, 2)),
  );
  let config;
  const saved = decrypt(cookies.ecmp_center);
  if (!sessions.activeSession(saved, sessions)) throw Error('expired');
  const users = require('./ui-users.cjs');
  const user = users.session(saved);
  if (!user || !['browser_workspace', 'shops'].some((p) => users.can(user, p, true))) throw Error('permission');
  config = { url: process.env.ECMP_CENTER_INTERNAL_URL || 'http://127.0.0.1:8010', token: users.centerToken() };
  if (!config) throw Error('authentication');
  let node = Number(match[1] || 0);
  if (!match[1] && cookies.pdd_active_node) {
    try {
      const n = decrypt(cookies.pdd_active_node);
      if (n.explicit === true && n.url === config.url && Number.isSafeInteger(n.id) && n.id > 0) node = n.id;
    } catch {}
  }
  const target = new URL(config.url);
  if (!['http:', 'https:'].includes(target.protocol)) throw Error('scheme');
  target.pathname = `/api/${node ? `nodes/${node}/proxy/` : ''}shops/${match[2]}/control`;
  target.search = '';
  target.hash = '';
  return { target, token: config.token, peerAddress: req.headers['x-ecmp-request-peer'] || '' };
}
function upgrade(req, socket, head) {
  if (!req.url.startsWith('/browser-control/')) return;
  let config;
  try {
    config = destination(req);
  } catch {
    socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');
    return;
  }
  const { target, token, peerAddress } = config;
  const upstream = (target.protocol === 'https:' ? https : http).request(target, {
    headers: {
      Host: target.host,
      Connection: 'Upgrade',
      Upgrade: 'websocket',
      'Sec-WebSocket-Key': req.headers['sec-websocket-key'],
      'Sec-WebSocket-Version': '13',
      Authorization: 'Bearer ' + token,
      ...(peerAddress ? { 'X-ECMP-Request-Peer': peerAddress } : {}),
    },
  });
  let peer;
  const timer = setTimeout(() => upstream.destroy(), 10000);
  socket.on('error', () => upstream.destroy());
  socket.on('close', () => {
    clearTimeout(timer);
    upstream.destroy();
    peer?.destroy();
  });
  upstream.on('upgrade', (res, remote, rest) => {
    clearTimeout(timer);
    peer = remote;
    if (socket.destroyed) {
      remote.destroy();
      return;
    }
    let headers = 'HTTP/1.1 101 Switching Protocols\r\n';
    for (const [k, v] of Object.entries(res.headers)) headers += `${k}: ${v}\r\n`;
    socket.write(headers + '\r\n');
    if (rest.length) socket.write(rest);
    if (head.length) remote.write(head);
    remote.on('error', () => socket.destroy());
    remote.on('close', () => socket.destroy());
    remote.pipe(socket);
    socket.pipe(remote);
  });
  upstream.on('response', (res) => {
    res.resume();
    socket.end(
      `HTTP/1.1 ${[401, 403, 404, 409].includes(res.statusCode) ? res.statusCode : 502} Remote unavailable\r\nConnection: close\r\n\r\n`,
    );
  });
  upstream.on('error', () => socket.destroy());
  upstream.end();
}
module.exports = { upgrade, destination };
