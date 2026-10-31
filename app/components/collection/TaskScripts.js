'use client';
import { PIPELINE, scriptsFor } from '../../lib/task-scripts.mjs';
import { useI18n } from '../../i18n/I18nProvider';

// Server-side scripts and how they work, shown inside the task layout editor.
export function StepScripts({ step, platform }) {
  const { t } = useI18n();
  const items = scriptsFor(step, platform);
  if (!items.length) return <p className="task-step-muted">{t('暂无脚本说明')}</p>;
  return (
    <div className="task-scripts-detail">
      {items.map((info) => (
        <div key={info.files.join('|') + info.principle}>
          <p>
            <b>{t('脚本')}：</b>
            {info.files.map((file) => (
              <code key={file} translate="no">
                {file}
              </code>
            ))}
          </p>
          <p>
            <b>{t('接口 / 页面')}：</b>
            <span>{t(info.api)}</span>
          </p>
          <p>
            <b>{t('原理')}：</b>
            {t(info.principle)}
          </p>
          {info.notes.map((note) => (
            <p key={note} className="task-step-muted">
              {t(note)}
            </p>
          ))}
        </div>
      ))}
    </div>
  );
}

export function TaskScripts({ steps, platform }) {
  const { t } = useI18n();
  if (!steps.length) return null;
  return (
    <details className="task-scripts">
      <summary>{t('脚本与原理')}</summary>
      {steps.map((step) => (
        <div key={step.id} className="task-scripts-step">
          <strong>{step.label}</strong>
          <StepScripts step={step} platform={platform} />
        </div>
      ))}
    </details>
  );
}

export function PipelineScripts() {
  const { t } = useI18n();
  return (
    <details className="task-scripts task-scripts-pipeline">
      <summary>{t('所有后台任务共用的执行流程（脚本与原理）')}</summary>
      <ol>
        {PIPELINE.map(([file, text]) => (
          <li key={file}>
            <code translate="no">{file}</code> {t(text)}
          </li>
        ))}
      </ol>
    </details>
  );
}
