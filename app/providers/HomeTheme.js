'use client';
import { Card, ConfigProvider, Tooltip } from 'antd';
export default function HomeTheme({ active, children }) {
  return (
    <ConfigProvider
      theme={
        active
          ? {
              token: { colorPrimary: '#198b7c', colorInfo: '#198b7c', borderRadius: 10 },
              components: { Card: { bodyPadding: 18 }, Statistic: { contentFontSize: 28 } },
            }
          : {}
      }
    >
      {children}
    </ConfigProvider>
  );
}
export function HomeMetricCard({ onClick, children, title, ...props }) {
  const card = (
    <Card
      {...props}
      hoverable={!!onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.target === e.currentTarget && ['Enter', ' '].includes(e.key)) {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      {children}
    </Card>
  );
  return title ? <Tooltip title={title}>{card}</Tooltip> : card;
}
