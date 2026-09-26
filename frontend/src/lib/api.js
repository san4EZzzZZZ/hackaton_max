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
  generateRoute: (payload) =>
    request('/routes/generate', { method: 'POST', body: JSON.stringify(payload) }),
}
