import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// TuneSync 管理界面：构建产物直接落到后端 static 目录，由 FastAPI 同源托管。
export default defineConfig({
  base: '/',
  plugins: [vue()],
  build: {
    outDir: '../app/static',
    emptyOutDir: true,
    assetsDir: 'assets',
    chunkSizeWarningLimit: 2400,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['vue', 'vue-router'],
          element: ['element-plus', '@element-plus/icons-vue'],
          http: ['axios'],
        },
      },
    },
  },
  server: {
    port: 5273,
    strictPort: false,
    // 仅本地调试用：开发服务器把 /api 与 /ws 代理到后端 8066。
    // 生产构建不需要代理，前端与后端同源。
    proxy: {
      '/api': { target: 'http://127.0.0.1:8066', changeOrigin: true },
      '/ws': { target: 'http://127.0.0.1:8066', changeOrigin: true, ws: true },
    },
  },
})
