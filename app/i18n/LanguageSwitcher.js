'use client';
import { GlobalOutlined } from '@ant-design/icons';
import { LOCALES } from './locales.mjs';
import { useI18n } from './I18nProvider';

// Language names are always shown in their own script (translate="no").
export default function LanguageSwitcher({ className = '' }) {
  const { locale, setLocale } = useI18n();
  return (
    <label className={`language-switcher ${className}`} translate="no" title="Language / 语言 / 語言">
      <GlobalOutlined aria-hidden="true" />
      <select aria-label="Language / 语言 / 語言" value={locale} onChange={(e) => setLocale(e.target.value)}>
        {LOCALES.map((item) => (
          <option key={item.id} value={item.id}>
            {item.label}
          </option>
        ))}
      </select>
    </label>
  );
}
