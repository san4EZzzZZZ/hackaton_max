import styles from './StateBlocks.module.css'

export function Loading({ label = 'Загружаем…' }) {
  return (
    <div className={styles.center} role="status">
      <span className={styles.spinner} aria-hidden="true" />
      <p className={styles.label}>{label}</p>
    </div>
  )
}

export function ErrorState({ message, onRetry }) {
  return (
    <div className={styles.center} role="alert">
      <div className={styles.errorBadge}>⚠️</div>
      <p className={styles.errorText}>{message}</p>
      {onRetry && (
        <button type="button" className={styles.retry} onClick={onRetry}>
          Повторить попытку
        </button>
      )}
    </div>
  )
}

export function EmptyState({ title, hint, action }) {
  return (
    <div className={styles.center}>
      <div className={styles.errorBadge}>🚶</div>
      <p className={styles.emptyTitle}>{title}</p>
      {hint && <p className={styles.label}>{hint}</p>}
      {action}
    </div>
  )
}
