export function shopGroupPage(shops, requested = 1, size = 24) {
  const pages = Math.max(1, Math.ceil(shops.length / size));
  const page = Math.max(1, Math.min(pages, Number.isInteger(requested) ? requested : 1));
  return { page, pages, visible: shops.slice((page - 1) * size, page * size) };
}
