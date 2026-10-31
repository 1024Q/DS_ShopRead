export const selectedValues = (value) =>
  [
    ...new Set(
      String(value ?? '')
        .split(',')
        .map((v) => v.trim())
        .filter(Boolean),
    ),
  ].sort();
export const inScope = (value, id) => !selectedValues(value).length || selectedValues(value).includes(String(id));
export const validSelection = (value, options) =>
  selectedValues(value)
    .filter((id) => options.some((o) => String(o.id) === id))
    .join(',');
export const scopePlatform = (shops, ids, platform = '') => {
  const values = [...new Set(shops.filter((s) => inScope(ids, s.id)).map((s) => s.platform || 'pdd'))];
  return ids && values.length === 1 ? values[0] : selectedValues(platform).length === 1 ? platform : '';
};
