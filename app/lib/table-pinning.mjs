// Shared by React rendering and the native-table preferences adapter.
export function pinnedColumn(name, index) {
  return index === 0 || /^(运营|商品|推广商品|报名商品|商品与问题|商品 \/ 订单)$/.test(name);
}
export function columnClass(name, index, count) {
  return [
    pinnedColumn(name, index) ? 'pinned-column' : '',
    index === count - 1 && name === '操作' ? 'action-column' : '',
  ]
    .filter(Boolean)
    .join(' ');
}
