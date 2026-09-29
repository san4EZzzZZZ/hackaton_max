const REASONS = {
  1: 'Браузер не дал доступ к геолокации — выберите стартовую точку вручную.',
  2: 'Сигнал спутников слабый: координаты не пришли. Выберите точку вручную.',
  3: 'Геолозация не ответила за десять секунд. Выберите точку вручную.',
  4: 'Этот браузер не умеет определять местоположение.',
}

/**
 * One position fix, as `[lon, lat]` in the order the map wants.
 *
 * `maximumAge` accepts a fix from a minute ago: on a phone that already has a lock, waiting for a fresh
 * one is a slower answer to the same question, and the visitor is not moving faster than a minute.
 */
export function currentPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error(REASONS[4]))
      return
    }
    navigator.geolocation.getCurrentPosition(
      (position) =>
        resolve([position.coords.longitude, position.coords.latitude]),
      (error) => reject(new Error(REASONS[error.code] || 'Координаты не пришли.')),
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
    )
  })
}
