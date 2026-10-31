'use client';
import { useEffect, useId, useRef, useState } from 'react';
import { Select, Button, Space } from 'antd';
import { useAdminWorkspace } from '../../providers/AdminWorkspace';
import { selectedValues } from '../../lib/filter-scope.mjs';
import styles from './MultiSelect.module.css';
const classes = (value) =>
  value
    .split(/\s+/)
    .filter(Boolean)
    .map((name) => [name, styles[name]].filter(Boolean).join(' '))
    .join(' ');
function HomeMultiSelect({ value = '', onChange, options, placeholder = '全部', label = '筛选', disabled = false }) {
  const [open, setOpen] = useState(false),
    [search, setSearch] = useState(''),
    [alignRight, setAlignRight] = useState(false);
  const root = useRef(null),
    trigger = useRef(null),
    input = useRef(null),
    id = useId();
  const selected = selectedValues(value),
    items = options.map((o) => ({ ...o, value: String(o.value) }));
  const matches = items.filter((o) => `${o.label} ${o.value}`.toLowerCase().includes(search.trim().toLowerCase()));
  const names = items.filter((o) => selected.includes(o.value)).map((o) => o.label);
  const close = () => {
    setOpen(false);
    setSearch('');
  };
  useEffect(() => {
    if (!open) return;
    input.current?.focus();
    const outside = (e) => {
      if (!root.current?.contains(e.target)) close();
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const update = (values) => onChange([...new Set(values)].sort().join(','));
  const toggle = (v) => update(selected.includes(v) ? selected.filter((x) => x !== v) : [...selected, v]);
  const focusOption = (direction) => {
    const nodes = [...root.current.querySelectorAll('[role="option"]')];
    const index = nodes.indexOf(document.activeElement);
    nodes[
      index < 0 ? (direction === 1 ? 0 : nodes.length - 1) : (index + direction + nodes.length) % nodes.length
    ]?.focus();
  };
  return (
    <div
      className={classes(`multi-select ${open ? 'is-open' : ''} ${names.length ? 'has-selection' : ''}`)}
      ref={root}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.preventDefault();
          e.stopPropagation();
          close();
          trigger.current?.focus();
        }
        if (open && ['ArrowDown', 'ArrowUp'].includes(e.key)) {
          e.preventDefault();
          focusOption(e.key === 'ArrowDown' ? 1 : -1);
        }
      }}
      onBlur={(e) => {
        if (open && !e.currentTarget.contains(e.relatedTarget)) close();
      }}
    >
      <button
        ref={trigger}
        type="button"
        className={classes('multi-select-trigger')}
        disabled={disabled}
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={id}
        title={names.join('、') || placeholder}
        onClick={() => {
          if (open) close();
          else {
            setAlignRight(window.innerWidth - trigger.current.getBoundingClientRect().left < 340);
            setOpen(true);
          }
        }}
      >
        <svg className={classes('multi-select-icon')} viewBox="0 0 20 20" aria-hidden="true">
          <path d="M3 5h14M5 10h10M8 15h4" />
        </svg>
        <span className={classes('multi-select-title')}>{names.length ? names[0] : placeholder}</span>
        {names.length > 1 && <b className={classes('multi-select-count')}>+{names.length - 1}</b>}
        <svg className={classes('multi-chevron')} viewBox="0 0 16 16" aria-hidden="true">
          <path d="m4 6 4 4 4-4" />
        </svg>
      </button>
      {open && (
        <div className={classes('multi-select-menu')} style={alignRight ? { left: 'auto', right: 0 } : undefined}>
          <div className={classes('multi-select-search')}>
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="8.5" cy="8.5" r="5.5" />
              <path d="m13 13 4 4" />
            </svg>
            <input
              ref={input}
              aria-label={`搜索${label}`}
              placeholder="输入名称搜索…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  if (matches[0]) toggle(matches[0].value);
                }
              }}
            />
            {search && (
              <button
                type="button"
                className={classes('multi-search-clear')}
                aria-label="清除搜索"
                onClick={() => {
                  setSearch('');
                  input.current?.focus();
                }}
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <path d="m4 4 8 8M12 4l-8 8" />
                </svg>
              </button>
            )}
          </div>
          <div className={classes('multi-select-actions')}>
            <button type="button" onClick={() => update([...selected, ...matches.map((o) => o.value)])}>
              全选{search ? '搜索结果' : ''}
            </button>
            <button type="button" onClick={() => update([])}>
              清空
            </button>
            <small>{selected.length ? `已选 ${selected.length} 项` : '全部范围'}</small>
          </div>
          <div
            id={id}
            role="listbox"
            aria-label={label}
            aria-multiselectable="true"
            className={classes('multi-select-options')}
          >
            {matches.map((o) => (
              <button
                key={o.value}
                type="button"
                role="option"
                aria-selected={selected.includes(o.value)}
                onClick={() => toggle(o.value)}
              >
                <span className={classes('multi-check')} aria-hidden="true">
                  {selected.includes(o.value) && (
                    <svg viewBox="0 0 16 16">
                      <path d="m3 8 3 3 7-7" />
                    </svg>
                  )}
                </span>
                <span className={classes('multi-option-label')} title={o.label}>
                  {o.label}
                </span>
              </button>
            ))}
            {!matches.length && <p className={classes('multi-select-empty')}>没有匹配项</p>}
          </div>
          <div className={classes('multi-select-footer')}>
            <span className={classes('multi-live-dot')} />
            勾选即生效<small>{search ? `匹配 ${matches.length} 项` : `共 ${items.length} 项`}</small>
          </div>
        </div>
      )}
    </div>
  );
}

export default function MultiSelect(props) {
  return useAdminWorkspace() ? <AdminMultiSelect {...props} /> : <HomeMultiSelect {...props} />;
}
function AdminMultiSelect({ value = '', onChange, options, placeholder = '全部', label = '筛选', disabled = false }) {
  const [search, setSearch] = useState('');
  const selected = selectedValues(value),
    items = options.map((o) => ({ ...o, value: String(o.value) }));
  const update = (values) => onChange([...new Set(values)].sort().join(','));
  return (
    <Select
      className={classes('admin-multi-select')}
      aria-label={label}
      mode="multiple"
      allowClear
      maxTagCount="responsive"
      disabled={disabled}
      value={selected}
      options={items}
      placeholder={placeholder}
      showSearch={{
        onSearch: setSearch,
        filterOption: (input, option) => `${option.label} ${option.value}`.toLowerCase().includes(input.toLowerCase()),
      }}
      onChange={update}
      popupRender={(menu) => (
        <>
          <Space className={classes('admin-multi-tools')}>
            <Button
              size="small"
              type="text"
              onClick={() =>
                update([
                  ...selected,
                  ...items
                    .filter((o) => `${o.label} ${o.value}`.toLowerCase().includes(search.toLowerCase()))
                    .map((o) => o.value),
                ])
              }
            >
              全选{search ? '搜索结果' : ''}
            </Button>
            <Button size="small" type="text" onClick={() => update([])}>
              清空
            </Button>
          </Space>
          {menu}
        </>
      )}
      style={{ minWidth: 180, width: '100%' }}
    />
  );
}
