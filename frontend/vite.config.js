import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 前端开发服务器，代理 /api 到后端 8001 端口
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
