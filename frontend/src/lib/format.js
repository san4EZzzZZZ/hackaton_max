export function formatMinutes(minutes) {
  if (minutes < 60) return `${minutes} мин`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} ч ${rest} мин` : `${hours} ч`
}

export function formatPrice(price) {
  if (!price) return 'Бесплатно'
  return `${Math.round(price).toLocaleString('ru-RU')} ₽`
}

export function pluralHours(n) {
  const rest10 = n % 10
  const rest100 = n % 100
  if (rest10 === 1 && rest100 !== 11) return 'час'
  if (rest10 >= 2 && rest10 <= 4 && (rest100 < 12 || rest100 > 14)) return 'часа'
  return 'часов'
}
