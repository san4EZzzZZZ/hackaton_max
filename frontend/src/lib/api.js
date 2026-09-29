const BASE = import.meta.env.VITE_API_URL || '/api/v1'

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
  } catch {
    throw new ApiError('Сервер недоступен. Проверьте соединение.', 0, null)
  }

  if (!response.ok) {
    let detail = null
    try {
      detail = (await response.json())?.detail
    } catch {
      /* body is not JSON — keep the status text */
    }
    throw new ApiError(
      detail || `Запрос завершился с ошибкой (${response.status})`,
      response.status,
      detail,
    )
  }
  return response.json()
}

export const api = {
  listCategories: () => request('/categories'),
  listChips: (city) =>
    request(`/chips${city ? `?city=${encodeURIComponent(city)}` : ''}`),
  /** Реальные места каталога — для поиска стартовой точки, а не для выдуманного списка. */
  searchPlaces: ({ city, q, limit = 8 }) => {
    const params = new URLSearchParams({ limit: String(limit) })
    if (city) params.set('city', city)
    if (q) params.set('q', q)
    return request(`/places?${params}`)
  },
  /** Справка о месте: история, факты для тех, кто стоит рядом. 404 — справки ещё нет. */
  getGuide: (placeId) => request(`/places/${encodeURIComponent(placeId)}/guide`),
  generateRoute: (payload) =>
    request('/routes/generate', { method: 'POST', body: JSON.stringify(payload) }),
}
