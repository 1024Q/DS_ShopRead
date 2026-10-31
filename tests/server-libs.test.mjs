import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { classifyRoute } from '../lib/proxy-routes.mjs';

const require = createRequire(import.meta.url);
const { createSessions, activeSession } = require('../lib/ui-sessions.cjs');
const { peerOf, trustedProxies } = require('../lib/request-peer.cjs');
const { LoginLimiter } = require('../lib/login-limiter.cjs');

test('logout revocation survives a fresh process and expires with the session', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ecmp-sessions-'));
  let now = 1000;
  const a = createSessions(dir, () => now);
  const id = a.newId();
  const saved = { token: 't', expires: 5000, sid: id };
  assert.equal(
    activeSession(saved, a, () => now),
    true,
  );
  a.revoke(id, saved.expires);
  assert.equal(
    activeSession(saved, a, () => now),
    false,
  );
  const b = createSessions(dir, () => now); // another module instance / restarted server
  assert.equal(b.isRevoked(id), true);
  now = 6000;
  assert.equal(b.isRevoked(id), false);
  assert.equal(
    activeSession(saved, b, () => now),
    false,
    'expired cookies stay invalid',
  );
  // Legacy cookies without a revocable session identity are rejected.
  assert.equal(
    activeSession({ token: 't', expires: 9000 }, b, () => now),
    false,
  );
});

test('peer address ignores X-Forwarded-For unless the socket is a configured proxy', () => {
  const req = (address, xff) => ({
    socket: { remoteAddress: address },
    headers: xff ? { 'x-forwarded-for': xff } : {},
  });
  assert.equal(peerOf(req('::ffff:172.16.22.9', '1.2.3.4'), trustedProxies('')), '172.16.22.9');
  const trusted = trustedProxies('10.0.0.1');
  assert.equal(peerOf(req('10.0.0.1', '6.6.6.6, 172.16.22.40'), trusted), '172.16.22.40');
  assert.equal(peerOf(req('10.0.0.1'), trusted), '10.0.0.1');
  assert.equal(peerOf(req('10.0.0.2', '172.16.22.40'), trusted), '10.0.0.2');
});

test('login limiter locks after five failures and a success resets the count', () => {
  let now = 0;
  const limiter = new LoginLimiter({ now: () => now });
  for (let i = 0; i < 5; i++) limiter.begin('a').finish(401);
  assert.ok(limiter.begin('a').retryAfter > 0);
  now = 900001;
  const attempt = limiter.begin('a');
  assert.equal(attempt.retryAfter, undefined);
  attempt.finish(200);
  limiter.begin('a').finish(401);
  assert.equal(limiter.begin('a').retryAfter, undefined);
});

test('proxy route table: allowed, denied and scope', () => {
  assert.equal(classifyRoute('fleet').scope, 'center');
  assert.equal(classifyRoute('shops/12/start').scope, 'node');
  assert.equal(classifyRoute('nodes/3/proxy/shops/12/start').scope, 'center');
  assert.equal(classifyRoute('nodes/3/proxy/nodes/4/proxy/shops'), null);
  assert.equal(classifyRoute('shops/../admin'), null);
  assert.equal(classifyRoute('admin/nodes'), null);
  assert.equal(classifyRoute('shops/12/unknown-action'), null);
});
