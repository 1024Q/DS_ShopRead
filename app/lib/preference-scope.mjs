let scope = '';
export const preferenceScope = () => scope;
export const preferenceKey = (key) => `${key}:machine:${scope || 'unconnected'}`;
export function setPreferenceScope(value) {
  const next = String(value || '');
  if (next === scope) return;
  scope = next;
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('preference-scope-ready'));
}
