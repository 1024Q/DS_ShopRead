export default [
  { ignores: ['node_modules/**', '.next*/**', '.data/**', '.runtime/**', '.venv/**', 'tmp/**', 'dist/**'] },
  {
    files: ['app/**/*.{js,mjs}', 'lib/**/*.{cjs,mjs}', 'server.cjs', 'tests/*.{cjs,mjs}', 'scripts/*.{cjs,mjs}'],
    languageOptions: { ecmaVersion: 'latest', parserOptions: { ecmaFeatures: { jsx: true } } },
    rules: {
      'constructor-super': 'error',
      'no-dupe-args': 'error',
      'no-dupe-class-members': 'error',
      'no-dupe-keys': 'error',
      'no-unreachable': 'error',
      'no-unsafe-finally': 'error',
      'valid-typeof': 'error',
    },
  },
];
