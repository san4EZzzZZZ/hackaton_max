import { useEffect, useRef } from 'react'
import { Map as GlMap, setWorkerUrl } from 'maplibre-gl'
import maplibreWorker from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import 'maplibre-gl/dist/maplibre-gl.css'
import PrimaryButton from '../components/PrimaryButton.jsx'
import { ArrowLeftIcon, NavigationIcon, SwapIcon } from '../components/icons.jsx'
import styles from './RouteResultScreen.module.css'

const ROSTOV_CENTER = [39.72, 47.235]

// Vite's dep-optimizer breaks maplibre's default worker URL resolution.
setWorkerUrl(maplibreWorker)

function stopCoord(stop) {
  return [stop.place.location.lon, stop.place.location.lat]
}

// The answer echoes the origin it planned from, so the map can show it without the setup screen
// having to hand its own selection over.
function startCoord(start) {
  return [start.lon, start.lat]
}

function formatDistance(meters) {
  // 950 m — порог, ниже которого «км» округлились бы в «0.0»: маршрут из двух кофеен рядом
  // выглядел бы нулевым. Выше порога те же значения округляются до «1.0 км».
  if (meters < 950) return `${Math.round(meters / 50) * 50} м`
  return `${(meters / 1000).toFixed(1)} км`
}

function formatHours(minutes) {
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return h > 0 ? `~ ${h} ч ${m} мин` : `~ ${m} мин`
}

export default function RouteResultScreen({ route, onEdit }) {
  const mapRef = useRef(null)
  const totalMinutes = Math.round(route.total_duration_hours * 60)
  const distanceLabel = formatDistance(route.total_distance_m)

  useEffect(() => {
    const map = new GlMap({
      container: mapRef.current,
      style: 'https://tiles.openfreemap.org/styles/positron',
      center: ROSTOV_CENTER,
      zoom: 12.5,
      attributionControl: false,
      scrollZoom: false,
    })
    const coords = route.stops.map(stopCoord)
    const start = route.start ? startCoord(route.start) : null
    // The line the guest walks starts where they stand, not at the first door.
    const path = start === null ? coords : [start, ...coords]
    map.on('load', () => {
      if (coords.length === 0) return
      map.addSource('route-line', {
        type: 'geojson',
        data: { type: 'Feature', geometry: { type: 'LineString', coordinates: path } },
      })
      map.addLayer({
        id: 'route-line',
        type: 'line',
        source: 'route-line',
        paint: { 'line-color': '#10B981', 'line-width': 4 },
      })
      map.addSource('route-stops', {
        type: 'geojson',
        data: {
          type: 'FeatureCollection',
          features: coords.map((c) => ({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: c },
          })),
        },
      })
      map.addLayer({
        id: 'route-stops',
        type: 'circle',
        source: 'route-stops',
        paint: {
          'circle-radius': 7,
          'circle-color': '#10B981',
          'circle-stroke-color': '#fff',
          'circle-stroke-width': 2.5,
        },
      })
      if (start !== null) {
        // Inverted colours: the origin must not read as a place to visit.
        map.addSource('route-start', {
          type: 'geojson',
          data: { type: 'Feature', geometry: { type: 'Point', coordinates: start } },
        })
        map.addLayer({
          id: 'route-start',
          type: 'circle',
          source: 'route-start',
          paint: {
            'circle-radius': 8,
            'circle-color': '#fff',
            'circle-stroke-color': '#10B981',
            'circle-stroke-width': 4,
          },
        })
      }
      const lons = path.map((c) => c[0])
      const lats = path.map((c) => c[1])
      map.fitBounds(
        [
          [Math.min(...lons), Math.min(...lats)],
          [Math.max(...lons), Math.max(...lats)],
        ],
        {
          padding: {
            top: 110,
            bottom: Math.round(window.innerHeight * 0.62) + 24,
            left: 48,
            right: 48,
          },
          duration: 0,
        },
      )
    })
    return () => map.remove()
  }, [route])

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
            <span className={styles.badge}>Центр Ростова</span>
            <h1 className={styles.title}>{route.title}</h1>
          </div>
          <div className={styles.stats}>
            <span className={styles.statMain}>{formatHours(totalMinutes)}</span>
            <span className={styles.statSub}>
              {distanceLabel} • {route.stops.length}{' '}
              {route.stops.length === 1 ? 'точка' : route.stops.length < 5 ? 'точки' : 'точек'}
            </span>
          </div>
        </div>

        {route.stops.length === 0 ? (
          <p className={styles.empty}>Маршрут пуст — попробуйте изменить время или интересы.</p>
        ) : (
          <ol className={styles.stops}>
            {route.start && route.stops.length > 0 && (
              <li className={styles.stop}>
                <span className={styles.startBadge} aria-hidden="true">
                  <NavigationIcon />
                </span>
                <div className={styles.stopText}>
                  <span className={styles.stopTitle}>Старт</span>
                  <span className={styles.stopSub}>
                    {`${route.stops[0].travel_minutes_from_prev} мин пешком • ` +
                      `${formatDistance(route.stops[0].distance_m_from_prev)} до первой точки`}
                  </span>
                </div>
              </li>
            )}
            {route.stops.map((stop) => (
              <li key={stop.place.id} className={styles.stop}>
                <span className={styles.order}>{stop.order}</span>
                <div className={styles.stopText}>
                  <span className={styles.stopTitle}>{stop.place.title}</span>
                  <span className={styles.stopSub}>
                    {(stop.place.description || stop.place.address || '') +
                      ` • ${stop.visit_duration_minutes} мин`}
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
          <PrimaryButton>Начать прогулку</PrimaryButton>
        </div>
      </div>
    </div>
  )
}
