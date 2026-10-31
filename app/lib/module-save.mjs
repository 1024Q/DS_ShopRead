export function assignmentMatches(config, item) {
  if (!config) return false;
  const field = {
    task: 'task_modes',
    step: 'task_modes',
    queue: 'platform_queues',
    frequency: 'platform_modes',
    priority: 'platform_priorities',
  }[item.tab];
  if (!field) return false;
  return config[field]?.[item.platform]?.[item.kind] === (item.target === 'default' ? undefined : item.target);
}
export function confirmedAssignments(items, data) {
  if (data?.node_configs?.some((node) => node.online === false || node.error || !node.collection)) return [];
  if (data?.scope_node_id === 0 && !data.node_configs?.length) return [];
  const configs = data?.node_configs?.length ? data.node_configs.map((n) => n.collection) : [data?.collection];
  return items.filter((item) => configs.length && configs.every((c) => assignmentMatches(c, item)));
}
export async function settingsRequest(method = 'GET', body, timeout = 20000, target = null, path = 'system/settings') {
  const controller = new AbortController(),
    timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch('/api/' + path, {
      method,
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        ...(target !== null ? { 'x-ecmp-config-node': String(target) } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.detail || '节点未确认保存');
      error.results = data.results;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError')
      throw new Error('等待节点确认超时，可能部分已生效；未确认修改已保留，请核对后重试');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}
