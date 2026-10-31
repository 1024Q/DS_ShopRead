'use client';
import { useEffect } from 'react';
export default function useQueryLocation(view, filters, ready) {
  const encoded = JSON.stringify(filters);
  useEffect(() => {
    if (!ready) return;
    const url = new URL(location.href);
    if (url.searchParams.get('view') && url.searchParams.get('view') !== view) return;
    url.searchParams.set('view', view);
    url.searchParams.set('filters', encoded);
    if (url.href !== location.href) {
      history.replaceState(null, '', url);
      window.dispatchEvent(new Event('workspace-query-location'));
    }
  }, [view, encoded, ready]);
}
