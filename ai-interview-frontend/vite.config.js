import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        // TODO: 改成你自己 VM 的 IP 地址
        target: process.env.BACKEND_PROXY_TARGET || 'http://127.0.0.1:8006',
        changeOrigin: true
      },
      '/uploads': {
        // TODO: 改成你自己 VM 的 IP 地址
        target: process.env.BACKEND_PROXY_TARGET || 'http://127.0.0.1:8006',
        changeOrigin: true
      }
    }
  }
})
