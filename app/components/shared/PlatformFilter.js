'use client';
import { createContext, useContext } from 'react';
import MultiSelect from './MultiSelect';
import { platforms, exclusivePlatforms } from '../../lib/platforms.mjs';
export const PlatformFilterContext = createContext(null);
export default function PlatformFilter() {
  const scope = useContext(PlatformFilterContext);
  if (!scope) return null;
  const exclusive = exclusivePlatforms[scope.view];
  return (
    <label className="platform-filter">
      平台筛选
      <MultiSelect
        label="业务平台筛选"
        value={exclusive || scope.platform}
        disabled={!!exclusive}
        onChange={scope.onChange}
        placeholder="全部平台"
        options={Object.entries(platforms)
          .filter(([id]) => !exclusive || id === exclusive)
          .map(([value, label]) => ({ value, label }))}
      />
    </label>
  );
}
