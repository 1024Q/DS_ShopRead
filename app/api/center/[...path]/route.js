import { connection, sameOrigin, peerHeaders } from '../../_connection';
export const dynamic = 'force-dynamic';
async function handle(request, context) {
  if (!sameOrigin(request)) return Response.json({ detail: '来源不允许' }, { status: 403 });
  const config = await connection();
  if (!config) return Response.json({ detail: '请先登录' }, { status: 401 });
  if (config.user.role !== 'admin') return Response.json({ detail: '只有管理员可以管理中心服务' }, { status: 403 });
  const route = (await context.params).path.join('/');
  if (
    !['nodes', 'snapshots', 'data', 'jobs', 'enrollments'].includes(route) &&
    !/^(?:nodes|enrollments)\/[a-f0-9-]{36}$/.test(route)
  )
    return new Response(null, { status: 404 });
  try {
    const url = new URL('/admin/' + route, config.url);
    url.search = new URL(request.url).search;
    const response = await fetch(url, {
      method: request.method,
      cache: 'no-store',
      redirect: 'error',
      headers: { Authorization: 'Bearer ' + config.token, 'Content-Type': 'application/json', ...peerHeaders(request) },
      body: ['POST', 'PUT', 'DELETE'].includes(request.method) ? await request.text() : undefined,
      signal: AbortSignal.timeout(30000),
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        'Content-Type': response.headers.get('content-type') || 'application/json',
        'Cache-Control': 'no-store',
      },
    });
  } catch {
    return Response.json({ detail: '中心服务连接失败' }, { status: 502 });
  }
}
export { handle as GET, handle as POST, handle as PUT, handle as DELETE };
