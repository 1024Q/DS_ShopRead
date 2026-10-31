'use client';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
export default function FrequencyGroups({ config, onChange }) {
  const groups = config.frequency_groups || [];
  const edit = (id, key, value) =>
    onChange({ ...config, frequency_groups: groups.map((g) => (g.id === id ? { ...g, [key]: value } : g)) });
  const remove = (id, target) => {
    if (!target) return;
    onChange({
      ...config,
      frequency_groups: groups.filter((g) => g.id !== id),
      group_moves: { ...config.group_moves, [id]: target },
    });
  };
  return (
    <>
      <h4 className="setting-group-title">采集频率与任务超时</h4>
      {groups.map((g) => (
        <div className="frequency-editor" key={g.id}>
          <label>
            名称
            <TextInput required maxLength={30} value={g.name} onChange={(e) => edit(g.id, 'name', e.target.value)} />
          </label>
          <label>
            采集间隔（秒）
            <TextInput
              type="number"
              min="1"
              step="1"
              required
              value={g.seconds}
              onChange={(e) => edit(g.id, 'seconds', Number(e.target.value))}
            />
          </label>
          <label>
            每日定时（留空按间隔）
            <TextInput type="time" value={g.time || ''} onChange={(e) => edit(g.id, 'time', e.target.value)} />
          </label>
          <label>
            任务超时（秒）
            <TextInput
              type="number"
              min="1"
              step="1"
              required
              value={g.timeout}
              onChange={(e) => edit(g.id, 'timeout', Number(e.target.value))}
            />
          </label>
          <label>
            删除并迁移任务至
            <Choice value="" disabled={groups.length < 2} onChange={(e) => remove(g.id, e.target.value)}>
              <option value="">选择迁入组后删除</option>
              {groups
                .filter((x) => x.id !== g.id)
                .map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.name}
                  </option>
                ))}
            </Choice>
          </label>
        </div>
      ))}
      <ActionButton
        type="button"
        onClick={() =>
          onChange({
            ...config,
            frequency_groups: [
              ...groups,
              {
                id: 'frequency_' + crypto.randomUUID().replaceAll('-', ''),
                name: '新增频率 ' + (groups.length + 1),
                seconds: 300,
                timeout: 240,
                time: '',
              },
            ],
          })
        }
      >
        添加频率组
      </ActionButton>
      <p>删除分组与任务迁移将在点击保存后一起生效。任务超时从开始执行时计算。</p>
    </>
  );
}
