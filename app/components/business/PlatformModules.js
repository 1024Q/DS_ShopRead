import { Fragment } from 'react';
// Only an explicit filter promotes a platform module, never the first result.
export default function PlatformModules({ platform = '', common, specific = [] }) {
  const before = specific.filter((m) => platform && m.platform === platform);
  const after = specific.filter((m) => !platform || m.platform !== platform);
  const render = (m) => <Fragment key={m.id}>{m.content}</Fragment>;
  return (
    <>
      {before.map(render)}
      <Fragment key="common">{common}</Fragment>
      {after.map(render)}
    </>
  );
}
