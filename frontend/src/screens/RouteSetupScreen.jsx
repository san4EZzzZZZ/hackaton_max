import { useEffect, useState } from 'react'
import PrimaryButton from '../components/PrimaryButton.jsx'
import {
  AlertIcon,
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

const CITY = 'Ростов-на-Дону'

// How many catalog places the sheet shows before the guest starts typing.
const POPULAR_LIMIT = 12

// A start needs both halves of the pair or none at all — the server answers 422 to half of it.
const NO_START = { kind: 'none', label: 'Без точки старта', lat: null, lon: null }
const AUTO_START = { kind: 'geo', label: 'Текущая геопозиция', lat: null, lon: null }

const GEO_ERRORS = {
  1: 'Браузер не дал доступ к геопозиции. Разреши его или выбери точку из списка.',
  2: 'Сигнал недоступен — выбери точку из списка.',
  3: 'Местоположение не определилось за отведённое время — выбери точку из списка.',
}

export default function RouteSetupScreen({ onBack, onSubmit, initial }) {
  const [duration, setDuration] = useState(initial?.duration ?? 2)
  const [selected, setSelected] = useState(initial?.tags ?? [])
  const [chips, setChips] = useState([])
  const [chipsError, setChipsError] = useState(null)
  const [start, setStart] = useState(initial?.start ?? AUTO_START)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [draftStart, setDraftStart] = useState(AUTO_START)
  const [query, setQuery] = useState('')
  const [places, setPlaces] = useState(null)
  const [placesError, setPlacesError] = useState(null)
  const [geoStatus, setGeoStatus] = useState('idle')
  const [geoNotice, setGeoNotice] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)
  // 404 reads differently when an origin caused it, so the failure needs to remember what it sent.
  const [sentStart, setSentStart] = useState(false)

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const data = await api.listChips(CITY)
        if (active) {
          setChips(data)
          setChipsError(null)
        }
      } catch (error) {
        if (active) setChipsError(error)
      }
    }
    load()
    return () => {
      active = false
    }
  }, [])

  // The sheet searches the real catalog: short input is not a query but the popular list.
  useEffect(() => {
    if (!sheetOpen) return undefined
    const q = query.trim()
    const searching = q.length >= 2
    let active = true
    const timer = setTimeout(
      async () => {
        try {
          const data = await api.listPlaces(
            searching ? { city: CITY, q } : { city: CITY, limit: POPULAR_LIMIT },
          )
          if (active) {
            setPlaces(data)
            setPlacesError(null)
          }
        } catch (error) {
          if (active) setPlacesError(error)
        }
      },
      searching ? 250 : 0,
    )
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [sheetOpen, query])

  const openSheet = () => {
    setDraftStart(start)
    setQuery('')
    setPlaces(null)
    setSheetOpen(true)
  }

  const applyStart = () => {
    setStart(draftStart)
    setSheetOpen(false)
    // An unresolved automatic start keeps its message: the guest confirmed the option, not a position.
    const pendingGeo = draftStart.kind === 'geo' && draftStart.lat === null
    if (!pendingGeo) setGeoNotice(null)
  }

  const toggleChip = (id) => {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    )
  }

  // Returns the position or null; a failure has already left its message on the start card, so
  // callers only need the null to stop.
  const resolveGeo = async () => {
    if (!('geolocation' in navigator)) {
      setGeoNotice('Это окно не умеет определять местоположение — выбери точку из списка.')
      setGeoStatus('denied')
      return null
    }
    setGeoStatus('requesting')
    try {
      const position = await new Promise((resolve, reject) => {
        navigator.geolocation.getCurrentPosition(resolve, reject, {
          timeout: 8000,
          maximumAge: 60000,
        })
      })
      const { latitude, longitude } = position.coords
      setGeoStatus('granted')
      setGeoNotice(null)
      return { lat: latitude, lon: longitude }
    } catch (error) {
      setGeoStatus('denied')
      setGeoNotice(GEO_ERRORS[error.code] ?? 'Не удалось определить местоположение.')
      return null
    }
  }

  // Asking for permission on tap, not on mount: a prompt the guest did not request looks like a bug.
  const pickAuto = async () => {
    setDraftStart(AUTO_START)
    const coords = await resolveGeo()
    if (coords) setDraftStart({ ...AUTO_START, ...coords })
  }

  const handleSubmit = async () => {
    setSubmitting(true)
    setSubmitError(null)
    setGeoNotice(null)
    try {
      const known = start.lat !== null && start.lon !== null
      let coords = known ? { lat: start.lat, lon: start.lon } : null
      if (start.kind === 'geo' && coords === null) {
        coords = await resolveGeo()
        if (coords === null) return
        setStart((prev) => ({ ...prev, ...coords }))
      }
      // The pair goes to the server only when it is complete; half of it answers 422.
      const payload = coords === null ? {} : { start_lat: coords.lat, start_lon: coords.lon }
      setSentStart(coords !== null)
      await onSubmit({
        duration,
        tags: selected,
        start: coords === null ? NO_START : { ...start, ...coords },
        route: await api.generateRoute({
          city: CITY,
          tags: selected,
          duration_hours: duration,
          ...payload,
        }),
      })
    } catch (error) {
      setSubmitError(error)
    } finally {
      setSubmitting(false)
    }
  }

  const startValue = () => {
    if (start.kind !== 'geo' || geoStatus !== 'requesting') return start.label
    return 'Определяем местоположение…'
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
          {chipsError ? (
            <p className={styles.submitError} role="alert">
              Не удалось загрузить варианты занятий.
            </p>
          ) : chips.length === 0 ? (
            <p className={styles.subtitle}>Загружаем варианты…</p>
          ) : (
            <div className={styles.chips}>
              {chips.map(({ id, emoji, label }) => (
                <button
                  key={id}
                  type="button"
                  aria-pressed={selected.includes(id)}
                  className={`${styles.chip} ${selected.includes(id) ? styles.chipActive : ''}`}
                  onClick={() => toggleChip(id)}
                >
                  <span className={styles.chipEmoji} aria-hidden="true">
                    {emoji}
                  </span>
                  {label}
                </button>
              ))}
            </div>
          )}
        </section>

        <section className={styles.startCard}>
          <div className={styles.startIcon}>
            <NavigationIcon />
          </div>
          <div className={styles.startText}>
            <span className={styles.startLabel}>Старт</span>
            <span className={styles.startValue}>{startValue()}</span>
            {geoNotice && (
              <span className={styles.startNotice} role="alert">
                <AlertIcon className={styles.noticeIcon} />
                {geoNotice}
              </span>
            )}
          </div>
          <button type="button" className={styles.startChange} onClick={openSheet}>
            Изменить
          </button>
        </section>

        {submitError && (
          <div className={styles.submitError} role="alert">
            {submitError.status === 404
              ? sentStart
                ? 'От выбранной точки за это время не успеть ни до чего интересного. Выбери старт ближе к центру или построй маршрут без него.'
                : 'Не удалось собрать маршрут под эти условия. Попробуйте другое время или категории.'
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
                className={`${styles.row} ${draftStart.kind === 'geo' ? styles.rowActive : ''}`}
                onClick={pickAuto}
              >
                <span className={styles.rowIcon} aria-hidden="true">
                  <NavigationIcon />
                </span>
                <span className={styles.rowText}>
                  <span className={styles.rowLabel}>Определить автоматически</span>
                  <span className={geoNotice ? styles.rowAlert : styles.rowSub}>
                    {geoStatus === 'requesting'
                      ? 'Запрашиваем доступ к геопозиции…'
                      : geoNotice ?? 'Использовать GPS телефона'}
                  </span>
                </span>
                <span
                  className={`${styles.radio} ${draftStart.kind === 'geo' ? styles.radioOn : ''}`}
                  aria-hidden="true"
                />
              </button>

              <button
                type="button"
                className={`${styles.row} ${draftStart.kind === 'none' ? styles.rowActive : ''}`}
                onClick={() => setDraftStart(NO_START)}
              >
                <span className={styles.rowIcon} aria-hidden="true">
                  <PinIcon />
                </span>
                <span className={styles.rowText}>
                  <span className={styles.rowLabel}>Без точки старта</span>
                  <span className={styles.rowSub}>
                    Начнём с ближайшей подходящей точки, без перехода до неё
                  </span>
                </span>
                <span
                  className={`${styles.radio} ${draftStart.kind === 'none' ? styles.radioOn : ''}`}
                  aria-hidden="true"
                />
              </button>

              <p className={styles.sheetSection}>
                {query.trim().length >= 2 ? 'Результаты поиска' : 'Точки из каталога'}
              </p>
              {placesError ? (
                <p className={styles.searchEmpty}>
                  Не удалось загрузить точки каталога. Попробуйте ещё раз.
                </p>
              ) : places === null ? (
                <p className={styles.searchEmpty}>Загружаем точки…</p>
              ) : (
                places.map((place) => {
                  const active = draftStart.kind === 'place' && draftStart.id === place.id
                  return (
                    <button
                      key={place.id}
                      type="button"
                      className={`${styles.row} ${active ? styles.rowActive : ''}`}
                      onClick={() =>
                        setDraftStart({
                          kind: 'place',
                          id: place.id,
                          label: place.title,
                          lat: place.location.lat,
                          lon: place.location.lon,
                        })
                      }
                    >
                      <span className={styles.rowIcon} aria-hidden="true">
                        <PinIcon />
                      </span>
                      <span className={styles.rowText}>
                        <span className={styles.rowLabel}>{place.title}</span>
                        <span className={styles.rowSub}>{place.address || place.category}</span>
                      </span>
                      <span
                        className={`${styles.radio} ${active ? styles.radioOn : ''}`}
                        aria-hidden="true"
                      />
                    </button>
                  )
                })
              )}
              {places !== null && !placesError && places.length === 0 && (
                <p className={styles.searchEmpty}>Ничего не найдено. Попробуйте другой запрос.</p>
              )}
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
