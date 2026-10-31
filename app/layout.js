import { AntdRegistry } from '@ant-design/nextjs-registry';
import AdminTheme from './providers/AdminTheme';
import I18nProvider from './i18n/I18nProvider';
import './styles/index.css';
import './styles/auth.css';
export const metadata = {
  title: '多店铺数据工作台',
  description: '多店铺数据工作台：多平台店铺浏览器管理、数据采集与经营数据查看',
};
export default function Layout({ children }) {
  return (
    <html lang="zh-CN" data-theme="white" suppressHydrationWarning>
      <body>
        <AntdRegistry>
          <I18nProvider>
            <AdminTheme>{children}</AdminTheme>
          </I18nProvider>
        </AntdRegistry>
      </body>
    </html>
  );
}
