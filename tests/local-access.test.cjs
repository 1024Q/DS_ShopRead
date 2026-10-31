const test = require('node:test');
const assert = require('node:assert/strict');
const { destination, upgrade } = require('../lib/remote-upgrade.cjs');
const request = (remoteAddress) => ({
  headers: { host: 'localhost:3211', origin: 'http://localhost:3211' },
  socket: { remoteAddress },
  url: '/browser-control/nodes/1/shops/1',
});
test('remote control requires center login for LAN and loopback peers', () => {
  for (const address of ['192.168.1.20', '::ffff:10.0.0.5', '127.0.0.1', '::1', '::ffff:127.0.0.1', undefined]) {
    let response;
    upgrade(
      request(address),
      {
        end(value) {
          response = value;
        },
      },
      Buffer.alloc(0),
    );
    assert.match(response, /^HTTP\/1\.1 403 Forbidden\r\n/);
  }
});
test('cross-origin remote control is rejected before authentication', () => {
  const req = request('127.0.0.1');
  req.headers.origin = 'http://other.example';
  assert.throws(() => destination(req), /origin/);
});
test('remote control rejects extra query parameters', () => {
  const req = request('127.0.0.1');
  req.url += '?token=untrusted';
  assert.throws(() => destination(req), /path/);
});
