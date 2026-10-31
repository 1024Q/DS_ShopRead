// Passwords and user sessions stay on this UI server. Never send the center key to a browser.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { promisify } = require('node:util');
const { pages, can, firstPage } = require('./ui-permissions.cjs');
const scrypt = promisify(crypto.scrypt);
const options = { N: 131072, r: 8, p: 1, maxmem: 256 * 1024 * 1024 };
function createUsers(directory = process.env.ECMP_UI_DATA_DIR || path.join(process.cwd(), '.data')) {
  const file = path.join(directory, 'ui-users.json');
  function load() {
    try {
      const value = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (value.schema !== 1 || !Array.isArray(value.users)) throw Error('Invalid account store');
      return value;
    } catch (e) {
      if (e.code === 'ENOENT') return { schema: 1, users: [] };
      throw e;
    }
  }
  function transaction(change) {
    fs.mkdirSync(directory, { recursive: true });
    const lock = file + '.lock';
    const fd = fs.openSync(lock, 'wx', 0o600);
    const temp = `${file}.${crypto.randomUUID()}.tmp`;
    try {
      const store = load();
      const result = change(store);
      fs.writeFileSync(temp, JSON.stringify(store), { flag: 'wx', mode: 0o600 });
      fs.renameSync(temp, file);
      return result;
    } finally {
      fs.closeSync(fd);
      fs.unlinkSync(lock);
      try {
        fs.unlinkSync(temp);
      } catch (e) {
        if (e.code !== 'ENOENT') console.warn('账号临时文件清理延后');
      }
    }
  }
  const publicUser = ({ password, ...u }) => u;
  async function hash(password) {
    if (typeof password !== 'string' || password.length < 12 || password.length > 256)
      throw Error('密码须为12至256个字符');
    const salt = crypto.randomBytes(16).toString('hex');
    return { salt, digest: (await scrypt(password, salt, 32, options)).toString('hex') };
  }
  function fields(body) {
    const username = String(body.username || '').trim();
    if (!/^[a-zA-Z0-9_.@-]{3,64}$/.test(username)) throw Error('账号须为3至64位字母、数字或 _.@-');
    if (!['admin', 'user'].includes(body.role)) throw Error('账号角色无效');
    const permissions = {};
    for (const [key, value] of Object.entries(body.permissions || {})) {
      if (!Object.hasOwn(pages, key) || !['read', 'write'].includes(value)) throw Error('页面权限无效');
      if (body.role === 'user' && key === 'machines' && value === 'write') throw Error('页面机器管理操作仅限管理员');
      permissions[key] = value;
    }
    return { username, role: body.role, permissions, disabled: body.disabled === true };
  }
  return {
    initialized: () => load().users.length > 0,
    list: () => load().users.map(publicUser),
    session(saved) {
      if (!saved?.uid || !saved.sid || saved.expires <= Date.now()) return null;
      const u = load().users.find((u) => u.id === saved.uid && u.version === saved.version && !u.disabled);
      return u ? publicUser(u) : null;
    },
    async authenticate(username, password) {
      if (typeof username !== 'string' || typeof password !== 'string' || password.length > 256) return null;
      const u = load().users.find((u) => u.username.toLowerCase() === username.trim().toLowerCase());
      const salt = u?.password.salt || '0'.repeat(32);
      const digest = await scrypt(password, salt, 32, options);
      if (!u || u.disabled || !crypto.timingSafeEqual(digest, Buffer.from(u.password.digest, 'hex'))) return null;
      return publicUser(u);
    },
    async create(body, setup = false) {
      const attrs = fields({ ...body, ...(setup ? { role: 'admin', disabled: false, permissions: {} } : {}) });
      const password = await hash(body.password);
      return transaction((store) => {
        if (setup && store.users.length) throw Error('管理员已初始化，请登录');
        if (store.users.some((u) => u.username.toLowerCase() === attrs.username.toLowerCase()))
          throw Error('账号已存在');
        const user = { ...attrs, id: crypto.randomUUID(), version: 1, password };
        store.users.push(user);
        return publicUser(user);
      });
    },
    async update(id, body) {
      const attrs = fields(body),
        password = body.password ? await hash(body.password) : null;
      return transaction((store) => {
        const user = store.users.find((u) => u.id === id);
        if (!user) throw Error('账号不存在');
        if (store.users.some((u) => u.id !== id && u.username.toLowerCase() === attrs.username.toLowerCase()))
          throw Error('账号已存在');
        Object.assign(user, attrs, { version: user.version + 1 }, password ? { password } : {});
        if (!store.users.some((u) => u.role === 'admin' && !u.disabled)) throw Error('至少保留一个启用的管理员');
        return publicUser(user);
      });
    },
    async changePassword(id, oldPassword, password) {
      const current = load().users.find((u) => u.id === id);
      if (!current || !(await this.authenticate(current.username, oldPassword))) throw Error('当前密码不正确');
      const digest = await hash(password);
      return transaction((store) => {
        const user = store.users.find((u) => u.id === id);
        if (!user || user.version !== current.version) throw Error('账号已更新，请重新登录');
        user.password = digest;
        user.version += 1;
      });
    },
  };
}
function centerToken(directory = path.join(process.cwd(), '.data')) {
  if (process.env.ECMP_CENTER_ADMIN_TOKEN) return process.env.ECMP_CENTER_ADMIN_TOKEN;
  const config = JSON.parse(fs.readFileSync(path.join(directory, 'center', 'center.local.json'), 'utf8'));
  if (!config.ECMP_CENTER_ADMIN_TOKEN) throw Error('中心管理凭据未配置');
  return config.ECMP_CENTER_ADMIN_TOKEN;
}
module.exports = { ...createUsers(), createUsers, centerToken, can, firstPage };
