import { useState } from 'react'
import PrimaryButton from '../components/PrimaryButton.jsx'
import {
  ArrowLeftIcon,
  ClockIcon,
  CloseIcon,
  NavigationIcon,
  PinIcon,
  SearchIcon,
} from '../components/icons.jsx'
import { api } from '../lib/api.js'
import styles from './RouteSetupScreen.module.css'

const DURATIONS = [
  { hours: 1, label: '1 час' },
  { hours: 2, label: '2 часа' },
  { hours: 3, label: '3 часа' },
  { hours: 4, label: '3+ часа' },
]

const KNOWN_PLACES = [
  { name: 'Парк им. Максима Горького', address: 'ул. Большая Садовая, 45' },
  { name: 'Пушкинская улица', address: 'пересечение с пр. Ворошиловским' },
  { name: 'Дворец спорта', address: 'ул. Левобережная, 3' },
  { name: 'Театр Горького', address: 'ул. Большая Садовая, 27' },
]

const START_LABELS = {
  geo: 'Текущая геопозиция',
}

// Чипы из Frame 2 — настроения, а не категории каталога; на сабмите
// каждый маппится на реальные категории бэкенда.
const CHIPS = [
  { emoji: '☕', label: 'Взять кофе', categories: [] },
  {
    emoji: '🏛️',
    label: 'Культура',
    categories: ['Театр', 'Архитектура', 'Памятник', 'Галерея', 'Музей'],
  },
  { emoji: '🌳', label: 'Погулять в парке', categories: ['Парк'] },
  { emoji: '🍕', label: 'Перекусить', categories: [] },
  { emoji: '📸', label: 'Красивые фото', categories: [] },
]

export default function RouteSetupScreen({ onBack, onSubmit, initial }) {
  const [duration, setDuration] = useState(initial?.duration ?? 2)
  const [selected, setSelected] = useState(initial?.categories ?? [])
  const [start, setStart] = useState('geo')
  const [sheetOpen, setSheetOpen] = useState(false)
  const [draftStart, setDraftStart] = useState('geo')
  const [query, setQuery] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)

  const openSheet = () => {
    setDraftStart(start)
    setQuery('')
    setSheetOpen(true)
  }

  const applyStart = () => {
    setStart(draftStart)
    setSheetOpen(false)
  }

  const toggleChip = (label) => {
    setSelected((prev) =>
      prev.includes(label) ? prev.filter((item) => item !== label) : [...prev, label],
    )
  }

  const handleSubmit = async () => {
    setSubmitting(true)
    setSubmitError(null)
    const picked = CHIPS.filter((chip) => selected.includes(chip.label)).flatMap(
      (chip) => chip.categories,
    )
    try {
      await onSubmit({
        duration,
        categories: selected,
        route: await api.generateRoute({
          city: 'Ростов-на-Дону',
          categories: picked,
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
        <span className={styles.stepBadge}>Шаг 1 из 2</span>
      </header>

      <div className="screen__body">
        <h1 className={styles.title}>Сколько у тебя времени?</h1>
        <p className={styles.subtitle}>Подберём точки, чтобы ты точно всё успел.</p>

        <section className={styles.section}>
          <div className={styles.durations} role="radiogroup" aria-label="Длительность">
            {DURATIONS.map(({ hours, label }) => (
              <button
                key={hours}
                type="button"
                role="radio"
                aria-checked={duration === hours}
                className={`${styles.duration} ${duration === hours ? styles.durationActive : ''}`}
                onClick={() => setDuration(hours)}
              >
                <ClockIcon className={styles.durationIcon} />
                <span className={styles.durationLabel}>{label}</span>
              </button>
            ))}
          </div>
        </section>

        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>Чем хочешь заняться?</h2>
          <div className={styles.chips}>
            {CHIPS.map(({ emoji, label }) => (
              <button
                key={label}
                type="button"
                aria-pressed={selected.includes(label)}
                className={`${styles.chip} ${selected.includes(label) ? styles.chipActive : ''}`}
                onClick={() => toggleChip(label)}
              >
                <span className={styles.chipEmoji} aria-hidden="true">
                  {emoji}
                </span>
                {label}
              </button>
            ))}
          </div>
        </section>

        <section className={styles.startCard}>
          <div className={styles.startIcon}>
            <NavigationIcon />
          </div>
          <div className={styles.startText}>
            <span className={styles.startLabel}>Старт</span>
            <span className={styles.startValue}>
              {START_LABELS[start] ?? start}
            </span>
          </div>
          <button type="button" className={styles.startChange} onClick={openSheet}>
            Изменить
          </button>
        </section>

        {submitError && (
          <div className={styles.submitError} role="alert">
            {submitError.status === 404
              ? 'Не удалось собрать маршрут под эти условия. Попробуйте другое время или категории.'
              : submitError.message}
          </div>
        )}
      </div>

      <div className={styles.footer}>
        <PrimaryButton onClick={handleSubmit} disabled={submitting}>
          {submitting ? 'Строим маршрут…' : 'Построить маршрут'}
        </PrimaryButton>
      </div>

      {sheetOpen && (
        <div className={styles.overlay} onClick={() => setSheetOpen(false)}>
          <div
            className={styles.sheet}
            role="dialog"
            aria-label="Выбор точки старта"
            onClick={(event) => event.stopPropagation()}
          >
            <span className={styles.sheetHandle} aria-hidden="true" />

            <div className={styles.searchRow}>
              <span className={styles.searchIcon} aria-hidden="true">
                <SearchIcon />
              </span>
              <input
                className={styles.searchInput}
                type="text"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Поиск улицы, парка, ориентира…"
                aria-label="Поиск места"
              />
              <button
                type="button"
                className={styles.searchClose}
                onClick={() => setSheetOpen(false)}
                aria-label="Закрыть"
              >
                <CloseIcon />
              </button>
            </div>

            <div className={styles.sheetBody}>
              <button
                type="button"
                className={`${styles.row} ${draftStart === 'geo' ? styles.rowActive : ''}`}
                onClick={() => setDraftStart('geo')}
              >
                <span className={styles.rowIcon} aria-hidden="true">
                  <PinIcon />
                </span>
                <span className={styles.rowText}>
                  <span className={styles.rowLabel}>Определить автоматически</span>
                  <span className={styles.rowSub}>Использовать GPS телефона</span>
                </span>
                <span
                  className={`${styles.radio} ${draftStart === 'geo' ? styles.radioOn : ''}`}
                  aria-hidden="true"
                />
              </button>

              <p className={styles.sheetSection}>Популярные точки в центре</p>
              {KNOWN_PLACES.filter(
                (place) =>
                  place.name.toLowerCase().includes(query.trim().toLowerCase()) ||
                  place.address.toLowerCase().includes(query.trim().toLowerCase()),
              ).map(({ name, address }) => (
                <button
                  key={name}
                  type="button"
                  className={`${styles.row} ${draftStart === name ? styles.rowActive : ''}`}
                  onClick={() => setDraftStart(name)}
                >
                  <span className={styles.rowIcon} aria-hidden="true">
                    <PinIcon />
                  </span>
                  <span className={styles.rowText}>
                    <span className={styles.rowLabel}>{name}</span>
                    <span className={styles.rowSub}>{address}</span>
                  </span>
                  <span
                    className={`${styles.radio} ${draftStart === name ? styles.radioOn : ''}`}
                    aria-hidden="true"
                  />
                </button>
              ))}
            </div>

            <div className={styles.sheetFooter}>
              <PrimaryButton onClick={applyStart}>Подтвердить выбор</PrimaryButton>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
