import { navigationGroups } from '../../lib/platforms.mjs';
import { useEffect, useRef, useState } from 'react';
import permissions from '../../../lib/ui-permissions.cjs';
export default function Navigation({ view, onChange, user }) {
  const groups = navigationGroups
    .map(([label, items, meta]) => [label, items.filter(([id]) => permissions.can(user, id)), meta])
    .filter(([, items]) => items.length);
  const key = view.startsWith('reconciliation_') ? 'reconciliation_day' : view;
  const active = groups.find(([, items]) => items.some(([id]) => id === key)) || groups[0];
  const lastActive = useRef(active?.[0]);
  const [expanded, setExpanded] = useState({}),
    [ready, setReady] = useState(false);
  useEffect(() => {
    try {
      setExpanded(JSON.parse(localStorage.getItem('commerce:navigation') || '{}'));
    } catch {}
    setReady(true);
  }, []);
  useEffect(() => {
    if (!active || lastActive.current === active[0]) return;
    lastActive.current = active[0];
    setExpanded((old) => ({ ...old, [active[0]]: true }));
  }, [active?.[0]]);
  const toggle = (label) =>
    setExpanded((old) => {
      const next = { ...old, [label]: !(old[label] ?? label === active[0]) };
      try {
        localStorage.setItem('commerce:navigation', JSON.stringify(next));
      } catch {}
      return next;
    });
  return (
    <aside className="workspace-sidebar">
      <button
        type="button"
        className="sidebar-brand"
        aria-label="返回首页"
        onClick={() => onChange(permissions.firstPage(user))}
      >
        <div>
          多店铺数据工作台<small>COMMERCE CONSOLE</small>
        </div>
      </button>
      <nav className="navigation-groups" aria-label="管理分类">
        {groups.map(([label, items, meta], index) =>
          meta?.standalone ? (
            <div className="nav-group nav-standalone" key={label}>
              <button
                className={`nav-category ${key === items[0][0] ? 'active' : ''}`}
                aria-label={label}
                aria-current={key === items[0][0] ? 'page' : undefined}
                onClick={() => onChange(items[0][0])}
              >
                <span className="nav-index">{String(index + 1).padStart(2, '0')}</span>
                {label}
              </button>
            </div>
          ) : (
            <div className="nav-group" key={label}>
              <button
                className={`nav-category ${active[0] === label ? 'active' : ''}`}
                aria-label={label}
                aria-expanded={!!(expanded[label] ?? label === active[0])}
                onClick={() => toggle(label)}
              >
                <span className="nav-index">{String(index + 1).padStart(2, '0')}</span>
                {label}
                <span className="nav-chevron">{(expanded[label] ?? label === active[0]) ? '−' : '+'}</span>
              </button>
              {(expanded[label] ?? label === active[0]) && (
                <nav className="main-tabs" aria-label="分类功能">
                  {items.map(([id, name]) => (
                    <button
                      key={id}
                      className={key === id ? 'active' : ''}
                      aria-current={key === id ? 'page' : undefined}
                      onClick={() => onChange(id)}
                    >
                      {name}
                    </button>
                  ))}
                </nav>
              )}
            </div>
          ),
        )}
      </nav>
      <div className="sidebar-note">店铺 · 数据 · 经营</div>
    </aside>
  );
}
