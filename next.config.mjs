/** @type {import('next').NextConfig} */
const config = {
  distDir: process.env.PDD_NEXT_DIST_DIR || '.next',
  poweredByHeader: false,
  outputFileTracingExcludes: {
    '/*': [
      './.data/**/*',
      './.pdd-*/**/*',
      './.browsers/**/*',
      './.venv/**/*',
      './.runtime/**/*',
      './pdd-login.local.txt',
    ],
  },
};
export default config;
