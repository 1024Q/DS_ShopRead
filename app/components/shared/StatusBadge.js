import { statusTone } from '../../lib/status-tone.mjs';
import styles from './StatusBadge.module.css';

export default function StatusBadge({ value, tone = statusTone(value), countdown = false }) {
  const classes = [
    'business-status',
    `tone-${tone}`,
    styles['business-status'],
    styles[`tone-${tone}`],
    countdown && 'pickup-countdown',
    countdown && styles['pickup-countdown'],
  ]
    .filter(Boolean)
    .join(' ');
  return <span className={classes}>{value || '—'}</span>;
}
