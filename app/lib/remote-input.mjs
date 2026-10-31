// Coalesce only adjacent motion events; never cross a press/release/key boundary.
export function appendInput(queue, event) {
  const last = queue.at(-1);
  if (event.type === 'mouseMoved' && last?.type === 'mouseMoved') queue[queue.length - 1] = event;
  else if (event.type === 'wheel' && last?.type === 'wheel')
    queue[queue.length - 1] = {
      ...event,
      delta_x: Math.max(-2000, Math.min(2000, (last.delta_x || 0) + (event.delta_x || 0))),
      delta_y: Math.max(-2000, Math.min(2000, (last.delta_y || 0) + (event.delta_y || 0))),
    };
  else queue.push(event);
  return queue;
}
export function controlPath(prefix, id) {
  const node = prefix.match(/^nodes\/(\d+)\/proxy\/$/);
  if (prefix && !node) throw Error('远程机器路径无效');
  return `/browser-control/${node ? `nodes/${node[1]}/` : ''}shops/${id}`;
}
