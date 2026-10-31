'use client';
import { useEffect, useState } from 'react';
import { selectedValues } from '../lib/filter-scope.mjs';
import { preferenceKey, preferenceScope } from '../lib/preference-scope.mjs';
export default function usePersistentSelection(key) {
  const [value, setValue] = useState(''),
    [ready, setReady] = useState(false);
  useEffect(() => {
    const restore = () => {
      if (!preferenceScope()) return;
      try {
        setValue(selectedValues(localStorage.getItem(preferenceKey('commerce:selection:' + key))).join(','));
      } catch {}
      setReady(true);
    };
    restore();
    window.addEventListener('preference-scope-ready', restore);
    return () => window.removeEventListener('preference-scope-ready', restore);
  }, [key]);
  useEffect(() => {
    if (ready)
      try {
        localStorage.setItem(preferenceKey('commerce:selection:' + key), value);
      } catch {}
  }, [key, value, ready]);
  return [value, setValue, ready];
}
