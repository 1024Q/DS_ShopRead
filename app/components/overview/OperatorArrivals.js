'use client';
import { ActionButton } from '../shared/AdminControls';
import { ComparisonArrow } from './YesterdayComparison';
import { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
function Calendar({ person, today, position }) {
  const [year, month] = today.split('-').map(Number),
    days = new Date(year, month, 0).getDate(),
    offset = (new Date(year, month - 1, 1).getDay() + 6) % 7;
  const peak = Math.max(1, ...Object.values(person.days || {}));
  return (
    <aside className="operator-calendar" role="tooltip" style={position}>
      <header>
        <strong>
          {person.name} · {month} 月上新
        </strong>
        <span>{year} 年</span>
      </header>
      <div className="operator-calendar-grid">
        {['一', '二', '三', '四', '五', '六', '日'].map((d) => (
          <small key={d}>{d}</small>
        ))}
        {Array.from({ length: offset }, (_, i) => (
          <span key={'empty' + i} />
        ))}
        {Array.from({ length: days }, (_, i) => {
          const date = `${year}-${String(month).padStart(2, '0')}-${String(i + 1).padStart(2, '0')}`,
            future = date > today,
            count = person.ready ? person.days?.[date] || 0 : null;
          return (
            <div
              key={date}
              className={`${date === today ? 'today ' : ''}${future ? 'future' : ''}`}
              style={{ '--intensity': future || !count ? 0 : 0.08 + (count / peak) * 0.26 }}
            >
              <span>{i + 1}</span>
              <b>{future ? '' : (count ?? '—')}</b>
            </div>
          );
        })}
      </div>
      <footer>
        按商品首次创建日期 · 已读取 {person.ready} / {person.shops.length} 家
      </footer>
    </aside>
  );
}
export default function OperatorArrivals({ people = [], day, today, year, onDetails }) {
  const [hover, setHover] = useState(null),
    timer = useRef(null);
  useEffect(() => {
    const close = () => setHover(null);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      clearTimeout(timer.current);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, []);
  const show = (event, person) => {
    clearTimeout(timer.current);
    const r = event.currentTarget.getBoundingClientRect();
    setHover({
      person,
      position: {
        position: 'fixed',
        left: Math.max(12, Math.min(r.left, window.innerWidth - 336)),
        ...(r.bottom + 365 < window.innerHeight ? { top: r.bottom + 8 } : { bottom: window.innerHeight - r.top + 8 }),
      },
    });
  };
  const ranked = [...people].sort((a, b) => (b.count ?? -1) - (a.count ?? -1) || a.name.localeCompare(b.name, 'zh-CN'));
  const max = Math.max(1, ...ranked.map((p) => p.count || 0));
  return (
    <div className="operator-arrivals operator-arrivals-refined">
      <div className="operator-arrival-heading">
        <h4>各运营上新</h4>
        <span>{ranked.length} 位 · 按上新数量排序</span>
      </div>
      <div
        className="operator-arrival-grid"
        style={{ '--operator-count': Math.max(1, ranked.length) }}
        role="group"
        aria-label="各运营上新统计，单排显示"
      >
        {ranked.map((person) => {
          const delta =
            person.count == null || person.previous == null
              ? null
              : {
                  comparable: person.ready,
                  direction: person.count > person.previous ? 'up' : person.count < person.previous ? 'down' : 'flat',
                  change: person.previous ? ((person.count - person.previous) / person.previous) * 100 : null,
                };
          return (
            <ActionButton
              className="operator-arrival-card"
              key={person.name}
              disabled={!person.shops.length}
              onMouseEnter={(e) => show(e, person)}
              onMouseLeave={() => setHover(null)}
              onFocus={(e) => show(e, person)}
              onBlur={() => setHover(null)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') setHover(null);
              }}
              onClick={() => onDetails(person.shops.join(','))}
              aria-label={`查看 ${person.name} 所负责店铺的上新商品`}
            >
              <div className="operator-card-heading">
                <div>
                  <strong title={person.name}>{person.name}</strong>
                  <small>{person.shops.length} 家店铺</small>
                </div>
                <ComparisonArrow metric={delta} date="前一日" />
              </div>
              <div className="operator-today">
                <span>{day === today ? '今日' : '所选日期'}上新</span>
                <strong>
                  {person.count ?? '—'}
                  <small>件</small>
                </strong>
              </div>
              <div className="operator-bar" aria-hidden="true">
                <i style={{ width: `${(Math.max(0, person.count || 0) / max) * 100}%` }} />
              </div>
              <div className="operator-secondary">
                <span>
                  {day === today ? '昨日' : '前一日'} <b>{person.previous ?? '—'}</b>
                </span>
                <span>
                  {Number(day.slice(5, 7))} 月{' '}
                  <b>{String(year) === day.slice(0, 4) ? (person.months?.[day.slice(0, 7)] ?? '—') : '—'}</b>
                </span>
              </div>
              {person.ready < person.shops.length && (
                <small className="operator-incomplete">
                  已读取 {person.ready} / {person.shops.length} 家
                </small>
              )}
            </ActionButton>
          );
        })}
      </div>
      {!people.length && <p className="data-note">暂无运营上新数据</p>}
      <p className="data-note">多人负责的店铺分别计入每位运营，人员数量不累加为总上新数。</p>
      {hover && createPortal(<Calendar person={hover.person} today={today} position={hover.position} />, document.body)}
    </div>
  );
}
