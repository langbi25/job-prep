import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base './' 使 dist 可被 FastAPI StaticFiles 直接托管（相对路径资源）
export default defineConfig({
  plugins: [vue()],
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8765', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
  },
})
