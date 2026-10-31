'use client';
import { useEffect, useState } from 'react';
import { ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import zhTW from 'antd/locale/zh_TW';
import { useI18n } from '../i18n/I18nProvider';
const ANTD_LOCALES = { 'zh-CN': zhCN, en: enUS, 'zh-TW': zhTW };
// Same accent as the CSS theme variables (--accent), so Ant Design and native controls match.
const ACCENTS = { white: '#3477e9', blue: '#2164de', dark: '#559aef' };
export default function AdminTheme({ children }) {
  const [themeName, setThemeName] = useState('white');
  const { locale } = useI18n();
  const dark = themeName === 'dark';
  useEffect(() => {
    const read = () => setThemeName(document.documentElement.dataset.theme || 'white');
    read();
    const observer = new MutationObserver(read);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);
  return (
    <ConfigProvider
      button={{ autoInsertSpace: false }}
      locale={ANTD_LOCALES[locale] || zhCN}
      theme={{
        algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: ACCENTS[themeName] || ACCENTS.white,
          colorSuccess: '#16836a',
          colorWarning: '#b97916',
          colorError: '#c44438',
          fontFamily: 'Arial,"Microsoft YaHei",sans-serif',
          fontSize: 13,
          borderRadius: 8,
          controlHeight: 36,
          colorBgLayout: dark ? '#172338' : '#f3f6fa',
        },
        components: {
          Table: { cellPaddingBlock: 12, cellPaddingInline: 14, headerBg: dark ? '#223249' : '#f6f8fc' },
          Button: { primaryShadow: 'none' },
          Card: { bodyPadding: 20 },
        },
      }}
    >
      {children}
    </ConfigProvider>
  );
}
