<!-- BEGIN:nextjs-agent-rules -->

## This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

## Collector releases

Do not rebuild or distribute the full collector package after routine feature changes. The user requested automatic collector updates. After validating collector changes, publish the signed code release using `.venv\Scripts\python.exe scripts\publish-collector-update.py`. Publishing is an explicit final step after tests, never a file watcher that publishes unfinished edits. The script refuses when packaged files (`backend/`, `config/`, launcher, quickstart) have uncommitted or untracked changes and runs the backend test suite first; commit first rather than passing `--allow-dirty`, and use `--skip-tests` only when the tests were just run. Preserve the center signing key in `.data/center/update-signing.local.pem`: a missing key is a hard error, never regenerated (`--init-key` is only for a brand-new center with no deployed collectors). If the key is passphrase-encrypted, set `ECMP_SIGNING_PASSPHRASE`. Never package private keys, node credentials, accounts or browser profiles. Use `--bootstrap` only to create the initial small patch for older collectors. Runtime/dependency changes require a separate compatible runtime upgrade; ordinary releases must remain database-backward-compatible for code rollback.

## Validation scope

Default to minimal testing directly covering the changed behavior, as requested by the user. Do not run the full suite unless necessary to resolve a specific risk.
