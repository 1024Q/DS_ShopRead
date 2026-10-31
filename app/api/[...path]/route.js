import { classifyRoute } from '../../../lib/proxy-routes.mjs';
import { connection, sameOrigin, activeNode, peerHeaders, encrypt, decrypt, secureCookie } from '../_connection';
import permissions from '../../../lib/ui-permissions.cjs';
import grants from '../../../lib/ui-export-grants.cjs';
import { cookies } from 'next/headers';
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
// Largest backend upload is 32 MiB (multipart); leave room for form overhead.
const MAX_BODY = 40 * 1024 * 1024;
async function proxy(request, context) {
  if (!sameOrigin(request)) return Response.json({ detail: '来源不允许' }, { status: 403 });
  const config = await connection();
  if (!config) return Response.json({ detail: '请先配置 API 连接' }, { status: 401 });
  const { path } = await context.params;
  const route = path.join('/');
  const routeConfig = classifyRoute(route);
  if (!routeConfig) return Response.json({ detail: '接口不存在' }, { status: 404 });
  const jar = await cookies();
  let savedGrants;
  try {
    savedGrants = decrypt(jar.get('ecmp_export_grants')?.value);
  } catch {}
  const download = /^(exports|table-tools\/exports)\/([a-f0-9]{32})(?:\/download)?$/.exec(route);
  if (
    !permissions.allowed(config.user, route, request.method, new URL(request.url).search) &&
    !(request.method === 'GET' && download && grants.valid(savedGrants, config.user, download[2], download[1]))
  )
    return Response.json({ detail: '没有此页面的查看或操作权限' }, { status: 403 });
  try {
    const requestedScope = request.headers.get('x-ecmp-config-node');
    if (
      requestedScope !== null &&
      (!['system/settings', 'system/concurrency', 'system/home-collection', 'system/clear-queue'].includes(route) ||
        !/^\d+$/.test(requestedScope) ||
        !Number.isSafeInteger(Number(requestedScope)))
    )
      return Response.json({ detail: '配置机器范围无效' }, { status: 400 });
    const node =
      requestedScope !== null ? Number(requestedScope) : routeConfig.scope === 'center' ? 0 : await activeNode(config);
    const target = node ? `nodes/${node}/proxy/${route}` : route;
    // The 60s timeout only covers waiting for response headers; streamed bodies (SSE, downloads) stay open until the client aborts.
    if (Number(request.headers.get('content-length') || 0) > MAX_BODY)
      return Response.json({ detail: '上传内容超过大小限制' }, { status: 413 });
    const body = request.method === 'GET' ? undefined : await request.arrayBuffer();
    if (body && body.byteLength > MAX_BODY) return Response.json({ detail: '上传内容超过大小限制' }, { status: 413 });
    let exportPage;
    if (route === 'table-tools/exports' && request.method === 'POST' && config.user.role !== 'admin') {
      try {
        exportPage = grants.tablePage(config.user, JSON.parse(new TextDecoder().decode(body)));
      } catch {}
      if (!exportPage) return Response.json({ detail: '没有此报表的导出权限' }, { status: 403 });
    }
    const headerTimeout = new AbortController(),
      timer = setTimeout(() => headerTimeout.abort(new DOMException('上游响应超时', 'TimeoutError')), 60000);
    let result;
    try {
      result = await fetch(`${config.url}/api/${target}${new URL(request.url).search}`, {
        method: request.method,
        headers: {
          Authorization: 'Bearer ' + config.token,
          'Content-Type': request.headers.get('content-type') || 'application/json',
          ...(request.headers.get('last-event-id') ? { 'Last-Event-ID': request.headers.get('last-event-id') } : {}),
          ...peerHeaders(request),
        },
        body,
        cache: 'no-store',
        redirect: 'error',
        signal: AbortSignal.any([request.signal, headerTimeout.signal]),
      });
    } finally {
      clearTimeout(timer);
    }
    const exportKind = /^data\/([^/]+)$/.exec(route)?.[1];
    const query = new URL(request.url).searchParams;
    const backgroundExport = exportKind && query.get('export') === 'true' && query.get('background') === 'true';
    if (config.user.role !== 'admin' && result.ok && (backgroundExport || route === 'table-tools/exports')) {
      const data = await result.json();
      if (Array.isArray(data))
        return Response.json(
          data.filter((j) => grants.valid(savedGrants, config.user, j.id, 'table-tools/exports')),
          { headers: { 'Cache-Control': 'no-store' } },
        );
      if (/^[a-f0-9]{32}$/.test(data.id)) {
        const page = exportPage || (exportKind === 'reconciliation_month' ? 'reconciliation_day' : exportKind);
        jar.set(
          'ecmp_export_grants',
          encrypt(
            grants.add(savedGrants, config.user, data.id, page, backgroundExport ? 'exports' : 'table-tools/exports'),
          ),
          { httpOnly: true, sameSite: 'strict', secure: secureCookie(request), path: '/', maxAge: 86400 },
        );
      }
      return Response.json(data, { status: result.status, headers: { 'Cache-Control': 'no-store' } });
    }
    if (route === 'events' && result.ok)
      return new Response(result.body, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' },
      });
    return new Response(result.body, {
      status: result.status,
      headers: {
        'Content-Type': result.headers.get('content-type') || 'application/json',
        'Cache-Control': 'no-store',
        ...(route.startsWith('invoice-imports/') && route.endsWith('/pdf')
          ? { 'Content-Security-Policy': "sandbox; default-src 'none'", 'X-Content-Type-Options': 'nosniff' }
          : {}),
        ...(result.headers.get('X-Query-Cache') ? { 'X-Query-Cache': result.headers.get('X-Query-Cache') } : {}),
        ...(result.headers.get('Server-Timing') ? { 'Server-Timing': result.headers.get('Server-Timing') } : {}),
        ...(result.headers.get('content-disposition')
          ? { 'Content-Disposition': result.headers.get('content-disposition') }
          : {}),
      },
    });
  } catch (error) {
    // Keep the three causes apart so the console message points at the real problem.
    if (request.signal.aborted) return new Response(null, { status: 499 });
    if (error?.name === 'TimeoutError' || error?.cause?.name === 'TimeoutError')
      return Response.json({ detail: '中心服务响应超时，请稍后重试' }, { status: 504 });
    return Response.json({ detail: 'Python API 无法连接，请检查服务是否运行' }, { status: 503 });
  }
}
export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
