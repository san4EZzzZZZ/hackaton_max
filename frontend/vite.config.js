import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxy = {
    '/api': {
      target: env.VITE_BACKEND_URL || 'http://127.0.0.1:8080',
      changeOrigin: true,
    },
  }
  return {
    plugins: [react()],
    worker: {
      // MapLibre's worker imports a sibling chunk; bundling it (not ?url copying) inlines
      // that dependency into one classic-format file so it loads under the CDN's MIME rules.
      format: 'iife',
    },
    server: {
      port: 5173,
      allowedHosts: ['.trycloudflare.com'],
      proxy,
    },
    // `vite preview` отдаёт собранный dist — прокси нужен и там, иначе прод-сборку не проверить локально.
    preview: { proxy },
  }
})
