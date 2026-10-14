import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies /api-less calls to the FastAPI backend so no CORS
// setup is needed during development. The built dist/ is served by FastAPI.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/resume': 'http://127.0.0.1:8000',
      '/jobs': 'http://127.0.0.1:8000',
      '/stats': 'http://127.0.0.1:8000',
      '/run': 'http://127.0.0.1:8000',
      '/runs': 'http://127.0.0.1:8000',
      '/scheduler': 'http://127.0.0.1:8000',
      '/settings': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/export.xlsx': 'http://127.0.0.1:8000',
      '/notify': 'http://127.0.0.1:8000',
      '/maintenance': 'http://127.0.0.1:8000',
    },
  },
})
