import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { classifyRoute } from '../lib/proxy-routes.mjs';
import limiterModule from '../lib/login-limiter.cjs';
import codecModule from '../lib/ui-crypto.cjs';
import peerModule from '../lib/request-peer.cjs';
import { uploadBody } from '../app/lib/upload-body.mjs';

test('routes retain center/node scope, reject unknown and nested proxy routes', () => {
  const id = 'a'.repeat(32);
  for (const route of [
    'fleet',
    'nodes/2',
    'cost-tables/preview',
    `report-exports/${id}/download`,
    'data/orders/complete-once',
    'profit/2/2026-09/confirm',
  ])
    assert.equal(classifyRoute(route)?.scope, 'center', route);
  for (const route of [
    'data/orders',
    'system/settings',
    'shops/2/start',
    'home/progress',
    `invoice-imports/${id}/files`,
  ])
    assert.equal(classifyRoute(route)?.scope, 'node', route);
  assert.equal(classifyRoute('nodes/2/proxy/system/settings')?.scope, 'center');
  for (const route of [
    '',
    'unknown',
    'nodes/2/proxy/nope',
    'nodes/2/proxy/nodes/3/proxy/logs',
    '../logs',
    'logs?token=x',
    'logs/',
    'shops/1/transfer-credentials',
  ])
    assert.equal(classifyRoute(route), null, route);
});

test('login failure blocks only that source and expires; network errors do not count', () => {
  let now = 0;
  const guard = new limiterModule.LoginLimiter({ now: () => now });
  for (let i = 0; i < 8; i++) guard.begin('a').finish(502);
  for (let i = 0; i < 5; i++) guard.begin('a').finish(401);
  assert.equal(guard.begin('a').retryAfter, 900);
  guard.begin('b').finish(200);
  now += 900001;
  guard.begin('a').finish(200);
  assert.equal(guard.sources.get('a').failures, 0);
});

test('pending logins cannot bypass failure limits, spoofed peer header is replaced', () => {
  const guard = new limiterModule.LoginLimiter();
  const pending = Array.from({ length: 5 }, () => guard.begin('a'));
  assert.equal(guard.begin('a').retryAfter, 1);
  pending.forEach((a) => a.finish(401));
  assert.equal(guard.begin('a').retryAfter, 900);
  const req = { headers: { 'x-ecmp-request-peer': 'spoof' }, socket: { remoteAddress: '192.168.1.2' } };
  peerModule.stampPeer(req);
  assert.equal(req.headers['x-ecmp-request-peer'], '192.168.1.2');
});

test('shared crypto reads existing cookies and cached key; rejects tampering', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ecmp-crypto-'));
  try {
    const key = crypto.randomBytes(32),
      iv = crypto.randomBytes(12),
      payload = { token: 'test-only', expires: 123 };
    fs.writeFileSync(path.join(dir, 'ui.key'), key);
    const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
    const legacy = Buffer.concat([
      iv,
      cipher.update(JSON.stringify(payload)),
      cipher.final(),
      cipher.getAuthTag(),
    ]).toString('base64url');
    const codec = codecModule.createCodec(dir);
    assert.deepEqual(codec.decrypt(legacy), payload);
    fs.renameSync(path.join(dir, 'ui.key'), path.join(dir, 'saved.key'));
    assert.deepEqual(codec.decrypt(codec.encrypt(payload)), payload);
    const altered = Buffer.from(legacy, 'base64url');
    altered[15] ^= 1;
    assert.throws(() => codec.decrypt(altered.toString('base64url')));
    assert.throws(() => codec.decrypt(''));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('upload helper preserves binary, filename, mapping and JSON actions', async () => {
  const bytes = new Uint8Array([0, 255, 13, 10, 128]);
  const file = new File([bytes], '成本.csv');
  const encoded = uploadBody({ file, filename: file.name, sku_column: 0, shipping_column: null });
  assert.equal(encoded.headers, undefined);
  const request = new Request('http://test/upload', { method: 'POST', ...encoded });
  assert.match(request.headers.get('content-type'), /^multipart\/form-data;/);
  const form = await request.formData();
  assert.equal(form.get('file').name, file.name);
  assert.deepEqual(new Uint8Array(await form.get('file').arrayBuffer()), bytes);
  assert.equal(form.get('sku_column'), '0');
  assert.equal(form.has('shipping_column'), false);
  assert.equal(uploadBody({ confirmed: true }).body, '{"confirmed":true}');
});
