import { Map as GlMap, setWorkerUrl } from 'maplibre-gl'
import maplibreWorker from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import 'maplibre-gl/dist/maplibre-gl.css'

// Vite's dep-optimizer breaks maplibre's default worker URL resolution.
setWorkerUrl(maplibreWorker)

const STYLE = 'https://tiles.openfreemap.org/styles/positron'

export const GREEN = '#10B981'
export const GREY = '#9CA3AF'
export const TARGET = '#2563EB'

export function createMap(container) {
  return new GlMap({
    container,
    style: STYLE,
    center: [39.72, 47.235],
    zoom: 12.5,
    attributionControl: false,
    scrollZoom: false,
  })
}

/**
 * A line between the given coordinates. Dashed means what it draws: a chord between two markers,
 * not a path anybody walked — the API said so in `geometry_source`.
 */
export function addLine(map, id, coordinates, { color = GREEN, width = 4, dashed = false } = {}) {
  map.addSource(id, {
    type: 'geojson',
    data: line(coordinates),
  })
  const layer = {
    id,
    type: 'line',
    source: id,
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': color, 'line-width': width },
  }
  if (dashed) layer.paint['line-dasharray'] = [2, 2]
  map.addLayer(layer)
}

export function setLine(map, id, coordinates) {
  const source = map.getSource(id)
  if (source) source.setData(line(coordinates))
}

/**
 * Пустой переход — это не линия из нуля точек, а отсутствие линии: MapLibre требует минимум две
 * координаты, а «проходить остаток маршрута» после последней точки нечего.
 */
function line(coordinates) {
  if (coordinates.length < 2) return { type: 'FeatureCollection', features: [] }
  return { type: 'Feature', geometry: { type: 'LineString', coordinates } }
}

export function addDots(map, id, points, color = GREEN) {
  map.addSource(id, {
    type: 'geojson',
    data: {
      type: 'FeatureCollection',
      features: points.map((point) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: point } })),
    },
  })
  map.addLayer({
    id,
    type: 'circle',
    source: id,
    paint: {
      'circle-radius': 7,
      'circle-color': color,
      'circle-stroke-color': '#fff',
      'circle-stroke-width': 2.5,
    },
  })
}

export function setDots(map, id, points) {
  const source = map.getSource(id)
  if (!source) return
  source.setData({
    type: 'FeatureCollection',
    features: points.map((point) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: point } })),
  })
}

export function fitTo(map, coordinates, bottomInset) {
  if (coordinates.length === 0) return
  const lons = coordinates.map((c) => c[0])
  const lats = coordinates.map((c) => c[1])
  map.fitBounds(
    [
      [Math.min(...lons), Math.min(...lats)],
      [Math.max(...lons), Math.max(...lats)],
    ],
    {
      padding: { top: 110, bottom: bottomInset, left: 48, right: 48 },
      duration: 0,
    },
  )
}
