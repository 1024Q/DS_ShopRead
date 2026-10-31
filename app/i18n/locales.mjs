export const DEFAULT_LOCALE = 'zh-CN';
export const LOCALES = [
  { id: 'zh-CN', label: '简体中文' },
  { id: 'en', label: 'English' },
  { id: 'zh-TW', label: '繁體中文' },
];
export const STORAGE_KEY = 'ecmp:locale';
export function normalizeLocale(value) {
  return LOCALES.some((l) => l.id === value) ? value : DEFAULT_LOCALE;
}
