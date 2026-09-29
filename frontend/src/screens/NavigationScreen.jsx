import { useEffect, useRef, useState } from 'react'
import PrimaryButton from '../components/PrimaryButton.jsx'
import { ArrowLeftIcon } from '../components/icons.jsx'
import { api } from '../lib/api.js'
import { formatMinutes, formatPrice } from '../lib/format.js'
import { addLine, createMap, fitTo, removeMarkers, setLine, syncMarkers, GREY } from '../lib/map.js'
import {
  formatTime,
  isHere,
  legCaption,
  pathLine,
  pointOf,
  remainingMinutes,
  walkedMeters,
  formatDistance,
} from '../lib/route.js'
import styles from './NavigationScreen.module.css'

const SHEET_INSET = () => Math.round(window.innerHeight * 0.5) + 24

export default function NavigationScreen({ route, onExit }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const markersRef = useRef(new Map())
  const [ready, setReady] = useState(false)
  const [nextIndex, setNextIndex] = useState(0)
  // walking — идем к точке; arrived — стояли у неё, читаем справку; done — прогулка закончена.
  const [phase, setPhase] = useState('walking')
  const [guide, setGuide] = useState(null)
  const [startedAt] = useState(() => new Date())
  const [finishedAt, setFinishedAt] = useState(null)

  const stops = route.stops
  const last = stops.length - 1
  const next = stops[nextIndex]
  const current = phase === 'done' ? [] : pathLine(route, nextIndex, nextIndex)
  const ahead = phase === 'done' ? [] : pathLine(route, nextIndex + 1, last)

  const markerState = (index) => {
    if (phase === 'done' || index < nextIndex) return 'reached'
    return index === nextIndex ? 'target' : 'idle'
  }

  // Тап по точке отвечает «я передумал и иду сюда»: маршрут тот же, меняется только шаг.
  const jumpTo = (_id, index) => {
    setNextIndex(index)
    setPhase('walking')
  }

  useEffect(() => {
    const map = createMap(containerRef.current)
    map.on('load', () => {
      addLine(map, 'path-ahead', ahead, { color: GREY, width: 3.5 })
      addLine(map, 'path-current', current, { width: 6 })
      mapRef.current = map
      setReady(true)
    })
    return () => {
      removeMarkers(markersRef.current)
      map.remove()
      mapRef.current = null
      setReady(false)
    }
    // Карта строится один раз на весь экран: пересобирать её на каждой смене точки — значит каждый раз
    // терять zoom и положение, которые посетитель задал себе пальцем.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !ready) return
    setLine(map, 'path-ahead', ahead)
    setLine(map, 'path-current', current)
    syncMarkers(
      map,
      markersRef.current,
      stops.map((stop, index) => ({
        id: stop.place.id,
        coord: pointOf(stop),
        label: String(index + 1),
        state: markerState(index),
        title: `Точка ${index + 1}: ${stop.place.title}`,
      })),
      phase === 'done' ? null : jumpTo,
    )
    fitTo(map, current.length >= 2 ? current : stops.map(pointOf), SHEET_INSET())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, nextIndex, phase])

  useEffect(() => {
    if (phase !== 'arrived' || !next) return
    let active = true
    setGuide(null)
    api
      .getGuide(next.place.id)
      .then((data) => {
        if (active) setGuide(data)
      })
      .catch(() => {
        /* справки об этом месте ещё нет — карточка живёт на описании из каталога */
      })
    return () => {
      active = false
    }
  }, [phase, next])

  const finish = () => {
    setFinishedAt(new Date())
    setPhase('done')
  }

  const onContinue = () => {
    if (nextIndex >= last) {
      finish()
      return
    }
    setNextIndex(nextIndex + 1)
    setPhase('walking')
  }

  const elapsedMinutes = Math.max(
    0,
    Math.round(((finishedAt ?? new Date()) - startedAt) / 60000),
  )
  const walked = walkedMeters(route, phase === 'done' ? stops.length : nextIndex)

  const nextSub = () => {
    if (nextIndex === 0 && !route.start) {
      return `первая точка маршрута • ${next.visit_duration_minutes} мин на месте`
    }
    if (isHere(next)) return 'вы уже здесь • осмотр начинается сразу'
    return `${legCaption(next)} • ≈${formatMinutes(next.travel_minutes_from_prev)} пешком`
  }

  return (
    <div className={styles.screen}>
      <div className={styles.map} ref={containerRef} />

      <button type="button" className={styles.back} onClick={onExit} aria-label="Назад к маршруту">
        <ArrowLeftIcon />
      </button>

      <span className={styles.progress}>
        {phase === 'done' ? 'Маршрут пройден' : `Точка ${nextIndex + 1} из ${stops.length}`}
      </span>

      <div className={styles.sheet}>
        {phase === 'walking' && next && (
          <>
            <div className={styles.timer}>
              <span className={styles.timerLabel}>Осталось времени</span>
              <span className={styles.timerValue}>{formatMinutes(remainingMinutes(route, nextIndex))}</span>
              <span className={styles.timerSub}>
                план на сейчас • старт в {formatTime(startedAt)}, прошло {formatMinutes(elapsedMinutes)}
              </span>
            </div>

            <div className={styles.next}>
              <span className={styles.nextLabel}>Следующая локация</span>
              <span className={styles.nextTitle}>{next.place.title}</span>
              <span className={styles.nextSub}>{nextSub()}</span>
            </div>

            <div className={styles.actions}>
              <PrimaryButton onClick={() => setPhase('arrived')}>Я на месте</PrimaryButton>
              <button type="button" className={styles.link} onClick={finish}>
                Завершить прогулку
              </button>
            </div>
          </>
        )}

        {phase === 'arrived' && next && (
          <div className={styles.card}>
            {next.place.image_url ? (
              <img className={styles.photo} src={next.place.image_url} alt="" loading="lazy" />
            ) : (
              <div className={styles.photoMissing}>Фото этого места не нашли</div>
            )}
            <span className={styles.cardBadge}>
              {next.place.category} • {formatPrice(next.place.price)} •{' '}
              {next.visit_duration_minutes} мин
            </span>
            <h2 className={styles.cardTitle}>{next.place.title}</h2>
            <p className={styles.cardText}>
              {guide?.highlights?.[0] ||
                next.place.description ||
                'Справки об этом месте ещё нет — она пишется вручную, по источникам.'}
            </p>
            {guide?.highlights?.length > 1 && (
              <p className={styles.cardMore}>{guide.highlights.slice(1).join(' • ')}</p>
            )}
            <div className={styles.actions}>
              <PrimaryButton onClick={onContinue}>
                {nextIndex >= last ? 'Завершить прогулку' : 'Далее к следующей точке'}
              </PrimaryButton>
              <button type="button" className={styles.link} onClick={onExit}>
                Выйти из навигации
              </button>
            </div>
          </div>
        )}

        {phase === 'done' && (
          <div className={styles.done}>
            <span className={styles.doneLabel}>Прогулка завершена</span>
            <span className={styles.doneValue}>{formatDistance(walked)}</span>
            <span className={styles.doneSub}>
              {stops.length} {stops.length === 1 ? 'точка' : 'точек'} • затратили{' '}
              {formatMinutes(elapsedMinutes)} • по плану {formatMinutes(route.total_duration_minutes)}
            </span>
            <div className={styles.actions}>
              <PrimaryButton onClick={onExit}>Вернуться к маршруту</PrimaryButton>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
