'use client';
import { useAdminWorkspace } from '../../providers/AdminWorkspace';
import { Children, isValidElement } from 'react';
import { Button, ConfigProvider, Input, Select } from 'antd';
// Keep existing controlled values and event handlers while replacing only presentation.
export function ActionButton({ type = 'button', className = '', children, ...props }) {
  const admin = useAdminWorkspace();
  const { componentDisabled } = ConfigProvider.useConfig();
  if (!admin)
    return (
      <button {...props} type={type} className={className}>
        {children}
      </button>
    );
  return (
    <Button
      {...props}
      disabled={props.disabled || componentDisabled}
      htmlType={type}
      className={className}
      type={className.split(' ').includes('primary') ? 'primary' : 'default'}
      danger={/\b(danger|danger-text|batch-delete|delete-action)\b/.test(className)}
    >
      {children}
    </Button>
  );
}
export function TextInput({ type = 'text', ...props }) {
  const admin = useAdminWorkspace();
  if (!admin) return <input type={type} {...props} />;
  if (['checkbox', 'radio', 'file', 'range', 'color'].includes(type)) return <input type={type} {...props} />;
  return <Input type={type} {...props} />;
}
const optionText = (value) =>
  Children.toArray(value)
    .map((c) => (isValidElement(c) ? optionText(c.props.children) : String(c)))
    .join('');
function optionsOf(children) {
  return Children.toArray(children).flatMap((c) => {
    if (!isValidElement(c)) return [];
    if (c.type === 'optgroup') return [{ label: c.props.label, options: optionsOf(c.props.children) }];
    if (c.type !== 'option') return [];
    return [
      {
        value: String(c.props.value ?? optionText(c.props.children)),
        label: c.props.children,
        disabled: c.props.disabled,
      },
    ];
  });
}
export function Choice({ children, value, onChange, style, ...props }) {
  const admin = useAdminWorkspace();
  const { componentDisabled } = ConfigProvider.useConfig();
  // A required native select participates in existing browser form validation.
  if (!admin || props.required || props.multiple)
    return (
      <select {...props} style={style} value={value} onChange={onChange}>
        {children}
      </select>
    );
  return (
    <Select
      {...props}
      disabled={props.disabled || componentDisabled}
      style={{ minWidth: 120, ...style }}
      value={value == null ? undefined : String(value)}
      options={optionsOf(children)}
      onChange={(v) => onChange?.({ target: { value: v }, currentTarget: { value: v } })}
      popupMatchSelectWidth={false}
    />
  );
}

export function TextArea(props) {
  const admin = useAdminWorkspace();
  return admin ? <Input.TextArea {...props} /> : <textarea {...props} />;
}

export function NativeButton({ type = 'submit', ...props }) {
  return <ActionButton {...props} type={type} />;
}
