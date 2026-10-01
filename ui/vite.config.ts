import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The console is served from /console in production, so asset URLs must be
// relative to that base rather than to the origin root.
export default defineConfig({
  plugins: [react()],
  base: '/console/',
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8088', changeOrigin: true },
      '/v1': { target: 'http://127.0.0.1:8088', changeOrigin: true },
    },
  },
})
