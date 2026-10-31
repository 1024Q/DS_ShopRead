export function commonValue(nodes, read) {
  if (!nodes.length || nodes.some((n) => !n.collection)) return undefined;
  const values = nodes.map((n) => read(n.collection));
  return values.every((v) => JSON.stringify(v) === JSON.stringify(values[0])) ? values[0] : undefined;
}
export function changedValue(nodes, key, value, patch) {
  const next = { ...patch };
  if (nodes.every((n) => n.collection && n.collection[key] === value)) delete next[key];
  else next[key] = value;
  return next;
}
export const scopeLabel = (data) =>
  data?.scope_node_id === 0
    ? `全部 ${data.node_configs?.length || 0} 台机器`
    : data?.node_configs?.[0]?.name || '当前机器';
