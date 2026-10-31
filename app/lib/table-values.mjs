import { Children, Fragment, isValidElement } from 'react';
export const elements = (value) =>
  Children.toArray(value).flatMap((node) =>
    isValidElement(node) && node.type === Fragment ? elements(node.props.children) : [node],
  );
// Read data/React values once. Never read innerText (which forces browser layout).
export function cellValue(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return node;
  if (Array.isArray(node)) return node.map(cellValue).join(' ');
  if (!isValidElement(node)) return '';
  if (Object.hasOwn(node.props, 'data-sort-value')) return node.props['data-sort-value'];
  if (node.props.row) return node.props.row.name || node.props.row.goods_id || '';
  return Children.toArray(node.props.children).map(cellValue).join(' ').trim();
}
export function rowValues(group) {
  if (group.props?.sortValues) return group.props.sortValues;
  const row = elements(group)[0];
  return elements(row?.props?.children).map(cellValue);
}
export function visibleRange(offsets, top, height, overscan = 6) {
  const count = offsets.length - 1;
  const find = (value) => {
    let low = 0,
      high = count;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (offsets[mid] < value) low = mid + 1;
      else high = mid;
    }
    return low;
  };
  return [Math.max(0, find(top) - 1 - overscan), Math.min(count, find(top + height) + overscan)];
}
