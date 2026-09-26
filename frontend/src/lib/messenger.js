// Thin bridge over messenger Mini App SDKs. MAX and Telegram expose different globals;
// everything is feature-detected so the app also runs in a plain browser tab.
const telegram = typeof window !== 'undefined' ? window.Telegram?.WebApp : undefined
const max = typeof window !== 'undefined' ? window.max : undefined

export function initMessenger() {
  telegram?.ready?.()
  telegram?.expand?.()
  max?.ready?.()
  max?.expand?.()
}

export function hapticTap() {
  if (telegram?.HapticFeedback) {
    telegram.HapticFeedback.impactOccurred('light')
  } else if (navigator.vibrate) {
    navigator.vibrate(8)
  }
}

export function isDark() {
  const theme = telegram?.colorScheme ?? max?.colorScheme
  if (theme) return theme === 'dark'
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
}
