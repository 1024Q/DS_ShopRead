'use client';
import { useEffect, useRef, useState } from 'react';
export default function DeferredSection({ children, label }) {
  const root = useRef(null),
    [visible, setVisible] = useState(false);
  useEffect(() => {
    const o = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true);
          o.disconnect();
        }
      },
      { rootMargin: '180px' },
    );
    o.observe(root.current);
    return () => o.disconnect();
  }, []);
  return (
    <div ref={root} style={{ minHeight: visible ? undefined : 100 }}>
      {visible ? children : <p className="data-note">{label}</p>}
    </div>
  );
}
