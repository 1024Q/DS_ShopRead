export function pickupCountdown(row, now = Date.now()) {
  if (row.platform !== 'pdd' || row.status !== '发货未揽件') return null;
  const due = Date.parse(row.pickup_deadline || '');
  if (!Number.isFinite(due)) return { text: '未提供发货时间', tone: 'gray', hours: null };
  const hours = (due - now) / 3600000;
  const amount = Math.abs(hours) < 0.1 ? '不足 0.1' : Math.abs(hours).toFixed(1);
  return {
    text: hours > 0 ? `剩余 ${amount} 小时` : hours === 0 ? '已到揽收期限' : `已超时 ${amount} 小时`,
    tone: hours <= 0 ? 'red' : hours <= 6 ? 'orange' : 'blue',
    hours,
  };
}
