const http = require('node:http');
const https = require('node:https'),
  fs = require('node:fs');
const dev = process.argv.includes('--dev');
const hostname = process.env.PDD_UI_HOST || '127.0.0.1',
  port = Number(process.env.PDD_UI_PORT || 3211);
const app = require('next')({ dev, hostname, port });
app
  .prepare()
  .then(() => {
    const handler = app.getRequestHandler();
    const listener = (req, res) => {
      require('./lib/request-peer.cjs').stampPeer(req);
      const url = new URL(req.url, 'http://localhost');
      if (url.pathname === '/' || url.pathname === '/center') {
        let user = null;
        try {
          const cookie = /(?:^|;\s*)ecmp_center=([^;]+)/.exec(req.headers.cookie || '')?.[1];
          const saved = require('./lib/ui-crypto.cjs').decrypt(cookie);
          const sessions = require('./lib/ui-sessions.cjs');
          if (sessions.activeSession(saved, sessions)) user = require('./lib/ui-users.cjs').session(saved);
        } catch {}
        const permissions = require('./lib/ui-permissions.cjs');
        if (url.pathname === '/' && !user) {
          res.writeHead(307, { Location: '/center', 'Cache-Control': 'no-store' }); res.end(); return;
        }
        if (user && (url.pathname === '/center' && user.role !== 'admin' || url.pathname === '/' && !permissions.can(user, url.searchParams.get('view') || 'overview'))) {
          const page = permissions.firstPage(user);
          if (!page) { res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' }); res.end('该账号尚未分配页面权限，请联系管理员。'); return; }
          res.writeHead(307, { Location: '/?view=' + page, 'Cache-Control': 'no-store' }); res.end(); return;
        }
      }
      handler(req, res);
    };
    const cert = process.env.ECMP_UI_TLS_CERT,
      key = process.env.ECMP_UI_TLS_KEY;
    if (Boolean(cert) !== Boolean(key)) throw Error('UI TLS requires both certificate and key');
    const server = cert
      ? https.createServer({ cert: fs.readFileSync(cert), key: fs.readFileSync(key) }, listener)
      : http.createServer(listener);
    server.on('upgrade', require('./lib/remote-upgrade.cjs').upgrade);
    server.listen(port, hostname, () =>
      console.log(`Manager: ${cert ? 'https' : 'http'}://${hostname}:${port} (${dev ? 'development' : 'production'})`),
    );
  })
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
