import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Forwards to the FastAPI backend so the browser sees same-origin
    // requests - avoids needing CORS middleware on the backend for local dev.
    proxy: {
      '/templates': 'http://localhost:8000',
      '/review': 'http://localhost:8000',
      '/results': 'http://localhost:8000',
    },
  },
})
