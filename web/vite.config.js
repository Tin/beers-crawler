import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev: proxy API paths to beers-crawler serve (default :8741)
const apiTarget = process.env.BEERS_CRAWLER_URL || 'http://127.0.0.1:8741'

// Production path on example.com — override with VITE_BASE if needed
const base = process.env.VITE_BASE || '/beers/rating/'

export default defineConfig({
  base,
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      // Local dev uses same-origin /beers/rating/api/* → backend /*
      '/beers/rating/api': {
        target: apiTarget,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/beers\/rating\/api/, ''),
      },
      // Keep bare /v1 + /health for simple local use without base path
      '/v1': { target: apiTarget, changeOrigin: true },
      '/health': { target: apiTarget, changeOrigin: true },
    },
  },
  preview: {
    port: 4173,
    proxy: {
      '/beers/rating/api': {
        target: apiTarget,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/beers\/rating\/api/, ''),
      },
      '/v1': { target: apiTarget, changeOrigin: true },
      '/health': { target: apiTarget, changeOrigin: true },
    },
  },
})
