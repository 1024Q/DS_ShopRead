'use client';
import { createContext, useContext, useMemo, Children, cloneElement, isValidElement, Fragment } from 'react';
const Context = createContext(new Map());
export function ShopOperatorsProvider({ shops, children }) {
  const names = useMemo(
    () => new Map(shops.map((s) => [String(s.id), s.operators?.length ? s.operators : s.operator ? [s.operator] : []])),
    [shops],
  );
  return <Context.Provider value={names}>{children}</Context.Provider>;
}
const flatten = (value) =>
  Children.toArray(value).flatMap((n) => (isValidElement(n) && n.type === Fragment ? flatten(n.props.children) : [n]));
export function useOperatorColumns(children, serverSort) {
  const names = useContext(Context);
  return useMemo(() => {
    const sections = Children.toArray(children),
      head = sections.find((n) => n.type === 'thead'),
      body = sections.find((n) => n.type === 'tbody');
    const header = Children.toArray(head?.props.children)[0],
      cells = Children.toArray(header?.props.children);
    const index = cells.findIndex((c) => typeof c.props?.children === 'string' && c.props.children.includes('店铺'));
    const rows = flatten(body?.props.children);
    const ids = (row) =>
      String(row.props?.['data-shop-ids'] || row.props?.['data-shop-id'] || '')
        .split(',')
        .filter(Boolean);
    if (
      index < 0 ||
      cells.some((c) => ['运营', '负责人'].includes(c.props?.children)) ||
      !rows.some((r) => ids(r).some((id) => names.get(id)?.length))
    )
      return { children, serverSort };
    const insert = (values) => {
      const next = Children.toArray(values);
      next.splice(index + 1, 0, <th key="shop-operators">运营</th>);
      return next;
    };
    function transform(node) {
      if (!isValidElement(node)) return node;
      if (node.type === Fragment) return cloneElement(node, {}, Children.map(node.props.children, transform));
      if (node.type !== 'tr') return node;
      const content = Children.toArray(node.props.children);
      if (content.length === 1 && content[0].props.colSpan > 1)
        return cloneElement(node, {}, cloneElement(content[0], { colSpan: content[0].props.colSpan + 1 }));
      const people = [...new Set(ids(node).flatMap((id) => names.get(id) || []))].join('、');
      content.splice(
        index + 1,
        0,
        <td key="shop-operators" className="shop-operator-cell" title={people || '未分配运营'} data-sort-value={people}>
          {people || '—'}
        </td>,
      );
      return cloneElement(node, {}, content);
    }
    const next = sections.map((s) =>
      s === head
        ? cloneElement(s, {}, cloneElement(header, {}, insert(header.props.children)))
        : s === body
          ? cloneElement(s, {}, Children.map(s.props.children, transform))
          : s,
    );
    const fields = serverSort ? [...(serverSort.fields || [])] : null;
    if (fields) fields.splice(index + 1, 0, 'operators');
    return { children: next, serverSort: serverSort ? { ...serverSort, fields } : serverSort };
  }, [children, serverSort, names]);
}
