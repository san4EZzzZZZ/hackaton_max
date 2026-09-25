import { useEffect, useState } from 'react'
import PrimaryButton from '../components/PrimaryButton.jsx'
import { ArrowLeftIcon } from '../components/icons.jsx'
import { ErrorState, Loading } from '../components/StateBlocks.jsx'
import { api } from '../lib/api.js'
import { pluralHours } from '../lib/format.js'
import styles from './RouteSetupScreen.module.css'

const DURATIONS = [1, 2, 3, 4]

export default function RouteSetupScreen({ onBack, onSubmit, initial }) {
  const [categories, setCategories] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [duration, setDuration] = useState(initial?.duration ?? 2)
  const [selected, setSelected] = useState(initial?.categories ?? [])
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoadError(null)
    api
      .listCategories()
      .then((list) => !cancelled && setCategories(list))
      .catch((error) => !cancelled && setLoadError(error))
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const toggleCategory = (name) => {
    setSelected((prev) =>
      prev.includes(name) ? prev.filter((item) => item !== name) : [...prev, name],
    )
  }

  const handleSubmit = async () => {
    setSubmitting(true)
    setSubmitError(null)
    try {
      await onSubmit({
        duration,
        categories: selected,
        route: await api.generateRoute({
          city: 'Ростов-на-Дону',
          categories: selected,
          duration_hours: duration,
        }),
      })
    } catch (error) {
      setSubmitError(error)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className={`screen ${styles.screen}`}>
      <header className={styles.header}>
        <button type="button" className={styles.back} onClick={onBack} aria-label="Назад">
          <ArrowLeftIcon />
        </button>
        <h1 className={styles.headerTitle}>Параметры прогулки</h1>
      </header>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Сколько времени есть?</h2>
        <div className={styles.durations} role="radiogroup" aria-label="Длительность">
          {DURATIONS.map((hours) => (
            <button
              key={hours}
              type="button"
              role="radio"
              aria-checked={duration === hours}
              className={`${styles.duration} ${duration === hours ? styles.durationActive : ''}`}
              onClick={() => setDuration(hours)}
            >
              <span className={styles.durationValue}>{hours}</span>
              <span className={styles.durationUnit}>{pluralHours(hours)}</span>
            </button>
          ))}
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Что интересно?</h2>
        {loadError && (
          <ErrorState
            message={loadError.message}
            onRetry={() => {
              setCategories(null)
              setLoadError(null)
              setReloadKey((key) => key + 1)
            }}
          />
        )}
        {!loadError && !categories && <Loading label="Загружаем категории…" />}
        {categories?.length === 0 && (
          <p className={styles.emptyHint}>Каталог пока пуст — маршрут соберём из всех мест.</p>
        )}
        {categories?.length > 0 && (
          <div className={styles.chips}>
            {categories.map((name) => (
              <button
                key={name}
                type="button"
                aria-pressed={selected.includes(name)}
                className={`${styles.chip} ${selected.includes(name) ? styles.chipActive : ''}`}
                onClick={() => toggleCategory(name)}
              >
                {name}
              </button>
            ))}
            <p className={styles.chipHint}>
              {selected.length === 0
                ? 'Можно ничего не выбирать — тогда возьмём лучшие места всех категорий.'
                : `Выбрано категорий: ${selected.length}`}
            </p>
          </div>
        )}
      </section>

      {submitError && (
        <div className={styles.submitError} role="alert">
          {submitError.status === 404
            ? 'Не удалось собрать маршрут под эти условия. Попробуйте другое время или категории.'
            : submitError.message}
        </div>
      )}

      <div className={styles.footer}>
        <PrimaryButton onClick={handleSubmit} disabled={submitting}>
          {submitting ? 'Строим маршрут…' : 'Показать маршрут'}
        </PrimaryButton>
      </div>
    </div>
  )
}
