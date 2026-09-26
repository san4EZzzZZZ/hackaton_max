import { useEffect, useRef } from 'react'
import { Map as GlMap, setWorkerUrl } from 'maplibre-gl'
import maplibreWorker from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import 'maplibre-gl/dist/maplibre-gl.css'
import PrimaryButton from '../components/PrimaryButton.jsx'
import { ArrowLeftIcon, SwapIcon } from '../components/icons.jsx'
import styles from './RouteResultScreen.module.css'

const ROSTOV_CENTER = [39.72, 47.235]

// Vite's dep-optimizer breaks maplibre's default worker URL resolution.
setWorkerUrl(maplibreWorker)

function routeDistanceKm(stops) {
  let km = 0
  for (let i = 1; i < stops.length; i += 1) {
    const a = stops[i - 1].place.location
    const b = stops[i].place.location
    const dLat = (((b.lat - a.lat) * Math.PI) / 180)
    const dLon = (((b.lon - a.lon) * Math.PI) / 180)
    const s =
      Math.sin(dLat / 2) ** 2 +
      Math.cos((a.lat * Math.PI) / 180) *
        Math.cos((b.lat * Math.PI) / 180) *
        Math.sin(dLon / 2) ** 2
    km += 12742 * Math.asin(Math.sqrt(s))
  }
  return km
}

function formatHours(minutes) {
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return h > 0 ? `~ ${h} ч ${m} мин` : `~ ${m} мин`
}

export default function RouteResultScreen({ route, onEdit }) {
  const mapRef = useRef(null)
  const totalMinutes = Math.round(route.total_duration_hours * 60)
  const distanceKm = routeDistanceKm(route.stops)

  useEffect(() => {
    const map = new GlMap({
      container: mapRef.current,
      style: 'https://tiles.openfreemap.org/styles/positron',
      center: ROSTOV_CENTER,
      zoom: 12.5,
      attributionControl: false,
      scrollZoom: false,
    })
    const coords = route.stops.map((stop) => [
      stop.place.location.lon,
      stop.place.location.lat,
    ])
    map.on('load', () => {
      if (coords.length === 0) return
      map.addSource('route-line', {
        type: 'geojson',
        data: { type: 'Feature', geometry: { type: 'LineString', coordinates: coords } },
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
      const lons = coords.map((c) => c[0])
      const lats = coords.map((c) => c[1])
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
              {distanceKm.toFixed(1)} км • {route.stops.length}{' '}
              {route.stops.length === 1 ? 'точка' : route.stops.length < 5 ? 'точки' : 'точек'}
            </span>
          </div>
        </div>

        {route.stops.length === 0 ? (
          <p className={styles.empty}>Маршрут пуст — попробуйте изменить время или интересы.</p>
        ) : (
          <ol className={styles.stops}>
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
          <p className={styles.attribution}>
            © OpenStreetMap contributors
          </p>
        </div>
      </div>
    </div>
  )
}
