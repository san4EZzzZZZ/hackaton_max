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
  listPlaces: ({ city, q, limit } = {}) => {
    const params = new URLSearchParams()
    if (city) params.set('city', city)
    // `q` asks for at least two characters on the server; a shorter query would answer 422.
    if (q && q.trim().length >= 2) params.set('q', q.trim())
    if (limit) params.set('limit', String(limit))
    const query = params.toString()
    return request(`/places${query ? `?${query}` : ''}`)
  },
  generateRoute: (payload) =>
    request('/routes/generate', { method: 'POST', body: JSON.stringify(payload) }),
}
