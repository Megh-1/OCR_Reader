import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The FastAPI backend. 8000 is the local default; API_TARGET points the dev server at
// another one (e.g. the Docker stack on http://localhost:8010) without editing this file.
const API_TARGET = process.env.API_TARGET ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    // Forwards to the FastAPI backend so the browser sees same-origin
    // requests - avoids needing CORS middleware on the backend for local dev.
    // Every backend route prefix the UI calls must be listed here AND in
    // frontend/nginx.conf, which does the same job in the container.
    proxy: {
      '/templates': API_TARGET,
      '/review': API_TARGET,
      '/results': API_TARGET,
      '/documents': API_TARGET,
    },
  },
})
