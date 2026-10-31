'use client';
import { ActionButton } from './AdminControls';
import { Children, cloneElement, isValidElement, useId, useState } from 'react';
function headingText(node) {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  return isValidElement(node) ? Children.toArray(node.props.children).map(headingText).join('') : '';
}
function titleOf(node) {
  if (!isValidElement(node)) return '';
  if (['h2', 'h3', 'h4', 'strong'].includes(node.type)) return headingText(node);
  return Children.toArray(node.props.children).map(titleOf).find(Boolean) || '';
}
export default function CollapsibleSection({
  children,
  className = '',
  onExpandedChange,
  defaultExpanded = true,
  ...props
}) {
  const [expanded, setExpanded] = useState(defaultExpanded),
    id = useId(),
    parts = Children.toArray(children),
    first = parts[0],
    title = titleOf(first) || props['aria-label'] || '模块';
  const toggle = (
    <ActionButton
      type="button"
      className="module-collapse"
      aria-label={`${expanded ? '收起' : '展开'}${title}`}
      aria-expanded={expanded}
      aria-controls={id}
      onClick={() => {
        setExpanded(!expanded);
        onExpandedChange?.(!expanded);
      }}
    >
      {expanded ? '收起' : '展开'} <span aria-hidden="true">{expanded ? '⌃' : '⌄'}</span>
    </ActionButton>
  );
  const header =
    isValidElement(first) && first.type === 'div' ? (
      cloneElement(first, { className: `${first.props.className || ''} module-heading` }, first.props.children, toggle)
    ) : (
      <div className="module-heading">
        {first}
        {toggle}
      </div>
    );
  return (
    <section {...props} className={`${className} collapsible-section`} data-collapsed={!expanded}>
      {header}
      <div id={id} className="collapsible-body" hidden={!expanded}>
        {expanded ? parts.slice(1) : null}
      </div>
    </section>
  );
}
