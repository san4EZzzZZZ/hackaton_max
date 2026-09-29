import { useEffect, useRef, useState } from 'react'
import PrimaryButton from '../components/PrimaryButton.jsx'
import { ArrowLeftIcon, NavigationIcon, SwapIcon } from '../components/icons.jsx'
import { formatMinutes } from '../lib/format.js'
import {
  addLine,
  addStartDot,
  createMap,
  dimLine,
  fitTo,
  removeMarkers,
  setLine,
  syncMarkers,
  GREY,
  GREY_DEEP,
  GREEN_DEEP,
  ORDER_GRADIENT,
} from '../lib/map.js'
import { formatDistance, legCaption, pathLine, pluralStops, pointOf, routePolyline } from '../lib/route.js'
import styles from './RouteResultScreen.module.css'

export default function RouteResultScreen({ route, onEdit, onStart }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const markersRef = useRef(new Map())
  const listRef = useRef(null)
  const [ready, setReady] = useState(false)
  const [activeId, setActiveId] = useState(null)
  // «по тротуарам» и «напрямую» — это одно и то же число из разных миров, и посетитель должен видеть,
  // из какого: API сам говорит, удалось ли получить линию у роутера.
  const measured = route.geometry_source === 'osrm'
  const distanceMeters = route.total_walk_distance_m ?? route.total_distance_m

  useEffect(() => {
    setActiveId(null)
    const map = createMap(containerRef.current)
    const line = routePolyline(route)
    const bottomInset = Math.round(window.innerHeight * 0.62) + 24
    map.on('load', () => {
      if (line.length >= 2) {
        addLine(map, 'route-line', line, {
          gradient: measured ? ORDER_GRADIENT : null,
          color: GREY,
          dashed: !measured,
          width: 6,
        })
        // Пустой до первого тапа: данные приходят из эффекта ниже.
        addLine(map, 'route-focus', [], {
          color: measured ? GREEN_DEEP : GREY_DEEP,
          dashed: !measured,
          width: 7.5,
        })
      }
      if (route.start) addStartDot(map, [route.start.lon, route.start.lat])
      fitTo(map, line.length >= 2 ? line : route.stops.map(pointOf), bottomInset)
      mapRef.current = map
      setReady(true)
    })
    return () => {
      removeMarkers(markersRef.current)
      map.remove()
      mapRef.current = null
      setReady(false)
    }
  }, [route, measured])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !ready) return
    syncMarkers(
      map,
      markersRef.current,
      route.stops.map((stop, index) => ({
        id: stop.place.id,
        coord: pointOf(stop),
        label: String(index + 1),
        state: stop.place.id === activeId ? 'active' : 'idle',
        title: `Точка ${index + 1}: ${stop.place.title}`,
      })),
      setActiveId,
    )
  }, [ready, activeId, route])

  // Тап по маркеру показывает не только карточку, но и сам путь до неё: на длинном маршруте
  // остальная линия — шум, и без затемнения её не отличить от нужного отрезка.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !ready || !map.getLayer('route-focus')) return
    const index = route.stops.findIndex((stop) => stop.place.id === activeId)
    const focus = index >= 0 ? pathLine(route, 0, index) : []
    setLine(map, 'route-focus', focus)
    dimLine(map, 'route-line', focus.length >= 2)
  }, [ready, activeId, route])

  // Тап по маркеру подсвечивает карточку и подвозит шторку к ней: на длинном маршруте подсветка
  // иначе остаётся за пределами видимого списка.
  useEffect(() => {
    if (!activeId) return
    for (const node of listRef.current?.children ?? []) {
      if (node.dataset.stop === activeId) {
        node.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
        break
      }
    }
  }, [activeId])

  return (
    <div className={styles.screen}>
      <div className={styles.map} ref={containerRef} />

      <button type="button" className={styles.back} onClick={onEdit} aria-label="Назад к настройке">
        <ArrowLeftIcon />
      </button>

      <div className={styles.sheet}>
        <span className={styles.handle} aria-hidden="true" />

        <div className={styles.topRow}>
          <div className={styles.topText}>
            <span className={styles.badge}>{route.city}</span>
            <h1 className={styles.title}>{route.title}</h1>
          </div>
          <div className={styles.stats}>
            <span className={styles.statMain}>{formatMinutes(route.total_duration_minutes)}</span>
            <span className={styles.statSub}>
              {formatDistance(distanceMeters)} {measured ? 'по тротуарам' : 'напрямую'} •{' '}
              {route.stops.length} {pluralStops(route.stops.length)}
            </span>
          </div>
        </div>

        {!measured && route.stops.length > 1 && (
          <p className={styles.notice}>
            Сервер маршрутов не ответил, поэтому линия проложена по прямой. Расстояние тоже не
            измерено: указано то, что видно на карте.
          </p>
        )}

        {route.stops.length === 0 ? (
          <p className={styles.empty}>Маршрут пуст — попробуйте изменить время или интересы.</p>
        ) : (
          <ol className={styles.stops} ref={listRef}>
            {route.start && (
              // Переход от старта — не остановка, а путь до первой двери: своя строка, чтобы
              // карточка места оставалась рассказом про место, а не про дорогу до него.
              <li className={styles.stop}>
                <span className={styles.startBadge} aria-hidden="true">
                  <NavigationIcon />
                </span>
                <div className={styles.stopText}>
                  <span className={styles.stopTitle}>Старт</span>
                  <span className={styles.stopSub}>
                    {`${legCaption(route.stops[0])} • ≈${formatMinutes(route.stops[0].travel_minutes_from_prev)} пешком`}
                  </span>
                </div>
              </li>
            )}
            {route.stops.map((stop, index) => (
              <li
                key={stop.place.id}
                data-stop={stop.place.id}
                className={`${styles.stop} ${stop.place.id === activeId ? styles.stopActive : ''}`}
              >
                <span className={styles.order}>{stop.order}</span>
                <div className={styles.stopText}>
                  <span className={styles.stopTitle}>{stop.place.title}</span>
                  <span className={styles.stopSub}>
                    {index === 0
                      ? `${stop.visit_duration_minutes} мин`
                      : `${legCaption(stop)} • ${stop.visit_duration_minutes} мин`}
                  </span>
                </div>
                <span className={styles.swap} aria-hidden="true">
                  <SwapIcon />
                </span>
              </li>
            ))}
          </ol>
        )}

        <div className={styles.footer}>
          <PrimaryButton onClick={onStart} disabled={route.stops.length === 0}>
            Начать прогулку
          </PrimaryButton>
        </div>
      </div>
    </div>
  )
}
