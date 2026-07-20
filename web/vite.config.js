import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev: proxy /v1 and /health to beers-crawler serve (default :8741)
const apiTarget = process.env.BEERS_CRAWLER_URL || 'http://127.0.0.1:8741'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/v1': { target: apiTarget, changeOrigin: true },
      '/health': { target: apiTarget, changeOrigin: true },
    },
  },
  preview: {
    port: 4173,
    proxy: {
      '/v1': { target: apiTarget, changeOrigin: true },
      '/health': { target: apiTarget, changeOrigin: true },
    },
  },
})
