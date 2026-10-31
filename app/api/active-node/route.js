import { cookies } from 'next/headers';
import { connection, activeNode, encrypt, sameOrigin, secureCookie, peerHeaders } from '../_connection';
import users from '../../../lib/ui-users.cjs';
export const runtime = 'nodejs';
export async function GET() {
  const c = await connection();
  return Response.json({ id: c ? await activeNode(c) : 0 });
}
export async function POST(request) {
  if (!sameOrigin(request)) return Response.json({ detail: '来源不允许' }, { status: 403 });
  const c = await connection();
  if (!c) return Response.json({ detail: '请先连接主服务' }, { status: 401 });
  if (!users.firstPage(c.user)) return Response.json({ detail: '尚未分配页面权限' }, { status: 403 });
  try {
    const { id } = await request.json();
    if (!Number.isSafeInteger(id) || id < 0) throw Error('机器编号无效');
    if (id) {
      const r = await fetch(`${c.url}/api/nodes/${id}/proxy/shops`, {
        headers: { Authorization: 'Bearer ' + c.token, ...peerHeaders(request) },
        cache: 'no-store',
        redirect: 'error',
        signal: AbortSignal.timeout(15000),
      });
      if (!r.ok) throw Error('机器暂不可用，请检查连接设置');
    }
    (await cookies()).set('pdd_active_node', encrypt({ id, url: c.url, explicit: true }), {
      httpOnly: true,
      sameSite: 'strict',
      secure: secureCookie(request),
      path: '/',
      maxAge: 604800,
    });
    return Response.json({ id });
  } catch (error) {
    return Response.json({ detail: error.message || '切换失败' }, { status: 400 });
  }
}
