import { connection, activeNode } from '../_connection';
import crypto from 'node:crypto';
export const runtime = 'nodejs';
export async function GET() {
  const config = await connection();
  const cacheScope = config
    ? crypto
        .createHash('sha256')
        .update(JSON.stringify([config.url, config.user.id, config.user.version, await activeNode(config)]))
        .digest('hex')
    : '';
  return Response.json(
    { connected: !!config, url: config?.url || '', cacheScope, center: true, user: config?.user || null },
    { headers: { 'Cache-Control': 'no-store' } },
  );
}
export async function POST() {
  return Response.json({ detail: '请通过中心登录页登录' }, { status: 403 });
}
