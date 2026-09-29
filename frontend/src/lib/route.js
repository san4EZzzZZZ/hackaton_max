import { formatMinutes } from './format.js'

// 950 m — порог, ниже которого «км» округлились бы в «0.0»: маршрут из двух кофеен рядом выглядел бы
// нулевым. Выше порога те же значения округляются до «1.0 км».
export function formatDistance(meters) {
  if (meters < 950) return `${Math.round(meters / 50) * 50} м`
  return `${(meters / 1000).toFixed(1)} км`
}

export function pluralStops(count) {
  const rest10 = count % 10
  const rest100 = count % 100
  if (rest10 === 1 && rest100 !== 11) return 'точка'
  if (rest10 >= 2 && rest10 <= 4 && (rest100 < 12 || rest100 > 14)) return 'точки'
  return 'точек'
}

export function pointOf(stop) {
  return [stop.place.location.lon, stop.place.location.lat]
}

/**
 * The line into one stop: what the routing server drew, or a chord between the two markers.
 *
 * The chord is not a guess about the pavement — it is what the API had when OSRM stayed silent, and it
 * is dashed for exactly that reason.
 */
export function legOf(stop, previous) {
  const measured = stop.geometry_from_prev
  if (measured && measured.length >= 2) return measured
  return previous ? [pointOf(previous), pointOf(stop)] : null
}

export function legMeters(stop) {
  return stop.walk_distance_m_from_prev ?? null
}

/** Переход нулевой: посетитель уже стоит в этой точке, идти некуда. */
export function isHere(stop) {
  return legMeters(stop) === 0
}

/** «862 м по тротуарам» when the gap was measured, «165 м напрямую» when it was not. */
export function legCaption(stop) {
  if (isHere(stop)) return 'вы уже здесь'
  const measured = legMeters(stop)
  if (measured === null) return `${formatDistance(stop.distance_m_from_prev)} напрямую`
  return `${formatDistance(measured)} по тротуарам`
}

/**
 * Склеенная линия переходов, которые заканчиваются в точках с индексами `fromIndex`..`toIndex`.
 *
 * Переходы хранятся по одному на точку, поэтому «остаток маршрута» и «вот этот шаг» — один и тот же
 * код с разными границами, а не вторая реализация склейки.
 *
 * `append` принимает список точек, всегда один и тот же: точка — это `[lon, lat]`, а не два числа,
 * иначе первая же точка превращает LineString в смесь чисел и массивов, и MapLibre молча не рисует
 * линию целиком.
 */
export function pathLine(route, fromIndex, toIndex) {
  const line = []
  const append = (points) => {
    line.push(...(line.length ? points.slice(1) : points))
  }
  if (route.start && fromIndex === 0) append([[route.start.lon, route.start.lat]])
  const last = Math.min(toIndex, route.stops.length - 1)
  for (let index = fromIndex; index <= last; index += 1) {
    const stop = route.stops[index]
    const leg = legOf(stop, index === 0 ? null : route.stops[index - 1])
    append(leg || [pointOf(stop)])
  }
  return line
}

export function routePolyline(route) {
  return pathLine(route, 0, route.stops.length - 1)
}

/** Minutes left of the plan once the visitor has finished everything before `nextIndex`. */
export function remainingMinutes(route, nextIndex) {
  if (nextIndex === 0) return route.total_duration_minutes
  const reached = route.stops[nextIndex - 1]
  return Math.max(0, route.total_duration_minutes - reached.arrival_offset_minutes - reached.visit_duration_minutes)
}

/** Meters covered so far, counting a chord as its length when nothing measured it. */
export function walkedMeters(route, nextIndex) {
  return route.stops
    .slice(0, nextIndex)
    .reduce((sum, stop) => sum + (stop.walk_distance_m_from_prev ?? stop.distance_m_from_prev), 0)
}

export function formatTime(date) {
  return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
}

export { formatMinutes }
