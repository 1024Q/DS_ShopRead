import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import accounts from '../lib/ui-users.cjs';
import permissions from '../lib/ui-permissions.cjs';
import grants from '../lib/ui-export-grants.cjs';

test('first administrator, hashed passwords, permission changes and fail-closed account state', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ecmp-account-'));
  try {
    const users = accounts.createUsers(directory);
    assert.equal(users.initialized(), false);
    const admin = await users.create({ username: 'owner', password: 'test-only-password' }, true);
    await assert.rejects(users.create({ username: 'restricted', role: 'user', password: 'test-only-password', permissions: { machines: 'write' } }), /仅限管理员/);
    await assert.rejects(users.create({ username: 'second', password: 'test-only-password' }, true), /已初始化/);
    assert.equal((await users.authenticate('OWNER', 'test-only-password')).id, admin.id);
    assert.equal(await users.authenticate('owner', 'incorrect'), null);
    assert.equal(fs.readFileSync(path.join(directory, 'ui-users.json'), 'utf8').includes('test-only-password'), false);
    assert.equal(users.list()[0].password, undefined);
    const reader = await users.create({
      username: 'reader',
      role: 'user',
      password: 'test-only-reader',
      permissions: { transactions: 'read' },
    });
    const saved = { uid: reader.id, version: reader.version, sid: 'session', expires: Date.now() + 10000 };
    assert.equal(users.session(saved).id, reader.id);
    assert.equal(users.session({ ...saved, uid: undefined }), null); // Old token-only login has no account identity.
    await users.update(reader.id, { ...reader, disabled: true });
    assert.equal(users.session(saved), null);
    assert.equal(await users.authenticate('reader', 'test-only-reader'), null);
    await assert.rejects(users.update(admin.id, { ...admin, role: 'user' }), /至少保留/);
    assert.equal(users.list().find((u) => u.id === admin.id).role, 'admin');
    await users.changePassword(admin.id, 'test-only-password', 'test-only-replacement');
    assert.equal(await users.authenticate('owner', 'test-only-password'), null);
    assert.equal((await users.authenticate('owner', 'test-only-replacement')).id, admin.id);
    fs.writeFileSync(path.join(directory, 'ui-users.json'), '{}');
    assert.throws(() => users.initialized());
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('page permissions authorize API data and actions separately, including explicit node paths', () => {
  const user = { role: 'user', permissions: { transactions: 'read', shops: 'read', todos: 'write' } };
  for (const route of ['data/transactions', 'nodes/7/proxy/data/transactions']) {
    assert.equal(permissions.allowed(user, route, 'GET'), true);
    assert.equal(permissions.allowed(user, route + '/sync', 'POST'), false);
  }
  for (const route of [
    'system/settings',
    'data/orders',
    'report-exports',
    'cost-tables',
    'profit',
    'exports/abc/download',
  ])
    assert.equal(permissions.allowed(user, route, 'GET'), false, route);
  assert.equal(permissions.allowed(user, 'attention/sync', 'POST'), true);
  assert.equal(permissions.allowed(user, 'shops/1/stop', 'POST'), false);
  assert.equal(permissions.allowed(user, 'nodes/3/proxy/shops/1/input', 'POST'), false);
  assert.equal(permissions.allowed(null, 'shops', 'GET'), false);
  assert.equal(permissions.can(user, 'accounts'), false);
  assert.equal(permissions.can({ role: 'admin' }, 'accounts'), true);
  assert.equal(permissions.allowed({ role: 'admin', disabled: true }, 'shops', 'GET'), false);
});

test('background exports require a user-bound grant and the original page permission', () => {
  const reader = { id: 'r', version: 1, role: 'user', permissions: { transactions: 'read' } };
  const saved = grants.add(null, reader, 'a'.repeat(32), 'transactions', 'exports');
  assert.equal(grants.valid(saved, reader, 'a'.repeat(32), 'exports'), true);
  assert.equal(grants.valid(saved, { ...reader, id: 'other' }, 'a'.repeat(32), 'exports'), false);
  assert.equal(grants.valid(saved, { ...reader, version: 2 }, 'a'.repeat(32), 'exports'), false);
  assert.equal(grants.valid(saved, { ...reader, permissions: {} }, 'a'.repeat(32), 'exports'), false);
  assert.equal(grants.valid(saved, reader, 'a'.repeat(32), 'table-tools/exports'), false);
  assert.equal(grants.tablePage(reader, { source: 'orders' }), null);
  assert.equal(grants.tablePage(reader, { rows: [] }), 'transactions');
});
