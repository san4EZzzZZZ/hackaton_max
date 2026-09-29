import { Map as GlMap, Marker, setWorkerUrl } from 'maplibre-gl'
import maplibreWorker from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import 'maplibre-gl/dist/maplibre-gl.css'

// Vite's dep-optimizer breaks maplibre's default worker URL resolution.
setWorkerUrl(maplibreWorker)

const STYLE = 'https://tiles.openfreemap.org/styles/positron'

export const GREEN = '#10B981'
export const GREEN_DEEP = '#065F46'
export const GREEN_PALE = '#34D399'
export const GREY = '#9CA3AF'
export const GREY_DEEP = '#4B5563'
export const START_COLOR = '#3B82F6'

/**
 * Порядок следования зашит в цвет: от светлого на старте к тёмному на финише. На маршруте из 8 точек
 * линии накладываются и идут по одним и тем же улицам — без градиента невозможно понять, какой отрезок
 * куда ведёт.
 */
export const ORDER_GRADIENT = [GREEN_PALE, '#059669', GREEN_DEEP]

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
 *
 * Белая подложка — не украшение: позитрон светлый, и без неё зелёная линия теряется на крышах
 * и на серых дорогах того же веса.
 */
export function addLine(
  map,
  id,
  coordinates,
  { color = GREEN, gradient = null, width = 5.5, dashed = false, casing = !dashed } = {},
) {
  map.addSource(id, { type: 'geojson', data: line(coordinates), lineMetrics: Boolean(gradient) })
  const layout = { 'line-cap': 'round', 'line-join': 'round' }
  if (casing) {
    map.addLayer({
      id: `${id}-casing`,
      type: 'line',
      source: id,
      layout,
      paint: { 'line-color': '#fff', 'line-width': width + 5, 'line-opacity': 0.95 },
    })
  }
  const paint = { 'line-width': width }
  // line-gradient и line-dasharray MapLibre не смешивает: пунктир живёт на line-color.
  if (gradient) paint['line-gradient'] = gradientPaint(gradient)
  else paint['line-color'] = color
  if (dashed) paint['line-dasharray'] = [2, 2]
  map.addLayer({ id, type: 'line', source: id, layout, paint })
}

function gradientPaint(colors) {
  const stops = []
  colors.forEach((color, index) => stops.push(index / (colors.length - 1), color))
  return ['interpolate', ['linear'], ['line-progress'], ...stops]
}

export function setLine(map, id, coordinates) {
  const source = map.getSource(id)
  if (source) source.setData(line(coordinates))
}

/** Увод линии на второй план: фокус — на подсвеченном отрезке, остальное должно мешать меньше. */
export function dimLine(map, id, dimmed) {
  for (const layer of [id, `${id}-casing`]) {
    if (map.getLayer(layer)) map.setPaintProperty(layer, 'line-opacity', dimmed ? 0.22 : 1)
  }
}

/**
 * Пустой переход — это не линия из нуля точек, а отсутствие линии: MapLibre требует минимум две
 * координаты, а «проходить остаток маршрута» после последней точки нечего.
 */
function line(coordinates) {
  if (coordinates.length < 2) return { type: 'FeatureCollection', features: [] }
  return { type: 'Feature', geometry: { type: 'LineString', coordinates } }
}

/** Точка старта — единственная, у которой нет номера: идти до неё не надо, от неё и начинают. */
export function addStartDot(map, coordinates) {
  map.addSource('start-point', {
    type: 'geojson',
    data: { type: 'Feature', geometry: { type: 'Point', coordinates } },
  })
  map.addLayer({
    id: 'start-point',
    type: 'circle',
    source: 'start-point',
    paint: {
      'circle-radius': 8,
      'circle-color': START_COLOR,
      'circle-stroke-color': '#fff',
      'circle-stroke-width': 3,
    },
  })
}

/**
 * Нумерованные точки маршрута — DOM-маркеры, а не circle-слой: только так номер остаётся читаемым
 * на любом зуме, а по точке можно попасть пальцем. Состояния (`idle` / `active` / `reached` /
 * `target`) развешиваются на `data-state`, visuals живут в global.css.
 *
 * Узел, который отдаётся MapLibre, — только про позицию: библиотека каждый кадр пишет в него
 * `transform` инлайном. Поэтому кружок вложен внутрь, и все `transform` / `transition` — на нём;
 * иначе анимация цвета и размера тянет за собой координаты, и точки отстают от карты при зуме.
 *
 * `registry` — Map, которую вызывающий держит рядом с картой: маркеры переживают перерисовку
 * React, а при смене маршрута лишние снимаются здесь же.
 */
export function syncMarkers(map, registry, points, onSelect) {
  const alive = new Set()
  points.forEach(({ id, coord, label, state, title }, index) => {
    alive.add(id)
    let entry = registry.get(id)
    if (!entry) {
      entry = { id }
      const element = document.createElement('button')
      element.type = 'button'
      element.className = 'stop-marker'
      entry.disc = document.createElement('span')
      entry.disc.className = 'stop-marker__disc'
      element.appendChild(entry.disc)
      element.addEventListener('click', (event) => {
        event.stopPropagation()
        if (entry.onSelect) entry.onSelect(entry.id, entry.index)
      })
      entry.element = element
      entry.marker = new Marker({ element, anchor: 'center' }).setLngLat(coord).addTo(map)
      registry.set(id, entry)
    }
    // Обработчик запаздывающий: экран навигации меняет замыкание на каждом шаге, а вешать
    // слушателя заново — значит копить подписки на одном и том же узле.
    entry.onSelect = onSelect
    entry.index = index
    entry.marker.setLngLat(coord)
    entry.disc.textContent = label
    entry.element.dataset.state = state
    entry.element.disabled = !onSelect
    entry.element.setAttribute('aria-label', title)
  })
  for (const [id, entry] of registry) {
    if (!alive.has(id)) {
      entry.marker.remove()
      registry.delete(id)
    }
  }
}

export function removeMarkers(registry) {
  for (const { marker } of registry.values()) marker.remove()
  registry.clear()
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
      padding: { top: 110, bottom: bottomInset, left: 56, right: 56 },
      maxZoom: 16.5,
      duration: 0,
    },
  )
}
