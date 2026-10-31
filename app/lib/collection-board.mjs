import { formatInterval } from './source-display.mjs';
export const frequencyDefaults = {
  realtime: '实时采集',
  daily: '每日采集',
  custom: '自定义采集间隔',
  custom2: '自定义频率 2',
  custom3: '自定义频率 3',
};
export const frequencyLabel = (config, id) =>
  id === 'off'
    ? '暂停采集'
    : `${config.frequency_names?.[id] || frequencyDefaults[id]} · ${id === 'daily' ? '每日 ' + config.daily_time : '每 ' + formatInterval(config[id + '_seconds'])}`;
export function selectedMode(config, platform, id) {
  return config.platform_modes?.[platform]?.[id] ?? (platform === 'pdd' ? config.module_modes?.[id] : undefined);
}
export function assignModule(config, platform, id, mode) {
  const modes = { ...(platform === 'pdd' ? config.module_modes : {}), ...config.platform_modes?.[platform] };
  if (mode === 'default') delete modes[id];
  else modes[id] = mode;
  return {
    ...config,
    ...(platform === 'pdd' ? { module_modes: modes } : {}),
    platform_modes: { ...config.platform_modes, [platform]: modes },
  };
}

export const priorityLabels = { high: '高优先级', medium: '中优先级', low: '低优先级' };
const coreModules = new Set([
  'overview',
  'transactions',
  'ad_reports',
  'products',
  'aftersales',
  'evidence',
  'tickets',
  'packages',
  'intercepts',
  'invoices',
]);
export const selectedPriority = (config, platform, id) =>
  config.platform_priorities?.[platform]?.[id] || (coreModules.has(id) ? 'high' : 'medium');
