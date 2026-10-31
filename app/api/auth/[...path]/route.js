import { cookies } from 'next/headers';
import users from '../../../../lib/ui-users.cjs';
import sessions from '../../../../lib/ui-sessions.cjs';
import limits from '../../../../lib/login-limiter.cjs';
import permissions from '../../../../lib/ui-permissions.cjs';
import { currentUser, encrypt, sameOrigin, secureCookie } from '../../_connection';
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
const reply = (body, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });
const local = (request) => ['127.0.0.1', '::1'].includes(request.headers.get('x-ecmp-request-peer'));
const hashSlots = (globalThis.__ecmpPasswordSlots ||= { active: 0 });
async function handle(request, context) {
  if (!sameOrigin(request)) return reply({ detail: '来源不允许' }, 403);
  const route = (await context.params).path.join('/');
  const method = request.method;
  let attempt,
    ownsSlot = false;
  try {
    const user = await currentUser();
    if (route === 'session' && method === 'GET')
      return reply({ user, initialized: users.initialized(), setupAllowed: local(request), pages: permissions.pages });
    if (route === 'logout' && method === 'POST') {
      const jar = await cookies();
      try {
        const saved = (await import('../../_connection')).decrypt(jar.get('ecmp_center')?.value);
        sessions.revoke(saved.sid, saved.expires);
      } catch {}
      jar.delete('ecmp_center');
      jar.delete('pdd_active_node');
      jar.delete('ecmp_export_grants');
      return reply({ ok: true });
    }
    if (route === 'users' || /^users\/[a-f0-9-]{36}$/.test(route)) {
      if (!user) return reply({ detail: '请先登录' }, 401);
      if (user.role !== 'admin') return reply({ detail: '只有管理员可以管理账号权限' }, 403);
      if (method === 'GET' && route === 'users') return reply(users.list());
      if (!['POST', 'PUT'].includes(method) || (method === 'PUT' && route === 'users'))
        return reply({ detail: '方法不允许' }, 405);
    } else if (route === 'password' && method === 'POST') {
      if (!user) return reply({ detail: '请先登录' }, 401);
    } else if (['login', 'setup'].includes(route) && method === 'POST') {
      if (route === 'setup' && (!local(request) || users.initialized()))
        return reply({ detail: '首次管理员设置仅允许在本机进行，且只能初始化一次' }, 403);
    } else return reply({ detail: '接口不存在' }, 404);
    if (Number(request.headers.get('content-length') || 0) > 16384) return reply({ detail: '请求内容过大' }, 413);
    attempt = limits.limiter.begin(request.headers.get('x-ecmp-request-peer') || 'unknown');
    if (attempt.retryAfter || hashSlots.active >= 2) return reply({ detail: '操作过于频繁，请稍后重试' }, 429);
    hashSlots.active++;
    ownsSlot = true;
    const raw = await request.text();
    if (Buffer.byteLength(raw) > 16384) return reply({ detail: '请求内容过大' }, 413);
    const body = JSON.parse(raw);
    if (route === 'password') {
      await users.changePassword(user.id, body.currentPassword, body.password);
      (await cookies()).delete('ecmp_center');
      attempt.finish(200);
      return reply({ ok: true });
    }
    if (route === 'users') {
      const result = await users.create(body);
      attempt.finish(200);
      return reply(result);
    }
    if (route.startsWith('users/')) {
      const result = await users.update(route.split('/')[1], body);
      attempt.finish(200);
      return reply(result);
    }
    const account =
      route === 'setup' ? await users.create(body, true) : await users.authenticate(body.username, body.password);
    if (!account) {
      attempt.finish(401);
      return reply({ detail: '账号或密码不正确' }, 401);
    }
    const expires = Date.now() + 8 * 3600000;
    (await cookies()).set(
      'ecmp_center',
      encrypt({ uid: account.id, version: account.version, sid: sessions.newId(), expires }),
      {
        httpOnly: true,
        sameSite: 'strict',
        secure: secureCookie(request),
        path: '/',
        maxAge: 8 * 3600,
      },
    );
    attempt.finish(200);
    return reply({
      user: account,
      next: account.role === 'admin' ? '/' : '/?view=' + (users.firstPage(account) || 'none'),
    });
  } catch (error) {
    attempt?.finish(401);
    const message =
      error.code === 'EEXIST'
        ? '账号正在保存，请稍后重试'
        : error instanceof SyntaxError
          ? '请求内容格式错误'
          : error.message;
    // Only validation messages are exposed; filesystem and deployment errors stay private.
    const safe = /^(密码|账号|页面权限|管理员|至少保留|当前密码)/.test(message);
    return reply({ detail: safe ? message : '账号服务暂不可用，请稍后重试' }, safe ? 400 : 503);
  } finally {
    attempt?.finish(500);
    if (ownsSlot) hashSlots.active--;
  }
}
export { handle as GET, handle as POST, handle as PUT };
