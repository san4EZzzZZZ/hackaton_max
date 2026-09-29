import { useEffect, useRef } from 'react'
import PrimaryButton from '../components/PrimaryButton.jsx'
import { ArrowLeftIcon, SwapIcon } from '../components/icons.jsx'
import { formatMinutes } from '../lib/format.js'
import { addDots, addLine, createMap, fitTo, GREEN, GREY } from '../lib/map.js'
import { formatDistance, legCaption, pluralStops, pointOf, routePolyline } from '../lib/route.js'
import styles from './RouteResultScreen.module.css'

const START_COLOR = '#3B82F6'

export default function RouteResultScreen({ route, onEdit, onStart }) {
  const mapRef = useRef(null)
  // «по тротуарам» и «напрямую» — это одно и то же число из разных миров, и посетитель должен видеть,
  // из какого: API сам говорит, удалось ли получить линию у роутера.
  const measured = route.geometry_source === 'osrm'
  const distanceMeters = route.total_walk_distance_m ?? route.total_distance_m

  useEffect(() => {
    const map = createMap(mapRef.current)
    const line = routePolyline(route)
    const bottomInset = Math.round(window.innerHeight * 0.62) + 24
    map.on('load', () => {
      if (line.length >= 2) {
        addLine(map, 'route-line', line, { dashed: !measured, color: measured ? GREEN : GREY })
      }
      addDots(map, 'route-stops', route.stops.map(pointOf))
      if (route.start) {
        addDots(map, 'route-start', [[route.start.lon, route.start.lat]], START_COLOR)
      }
      fitTo(map, line.length >= 2 ? line : route.stops.map(pointOf), bottomInset)
    })
    return () => map.remove()
  }, [route, measured])

  return (
    <div className={styles.screen}>
      <div className={styles.map} ref={mapRef} />

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
          <ol className={styles.stops}>
            {route.stops.map((stop, index) => (
              <li key={stop.place.id} className={styles.stop}>
                <span className={styles.order}>{stop.order}</span>
                <div className={styles.stopText}>
                  <span className={styles.stopTitle}>{stop.place.title}</span>
                  <span className={styles.stopSub}>
                    {index === 0 && !route.start
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
