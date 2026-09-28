import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 迁移已完成：新版就是 `/`，产物放 /assets/ 下。
// '/next' 路由在 dashboard.py 里保留作兼容别名（老书签/主屏图标仍能用）。
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 调试方便：产物不压缩（迁移期）。迁完可去掉。
    minify: false,
    sourcemap: true,
  },
  server: {
    port: 5173,
    // dev 时 API / 静态资源转发到本机 Python 仪表盘（8787）
    proxy: {
      '/api': 'http://127.0.0.1:8787',
      '/stream': 'http://127.0.0.1:8787',
      '/files': 'http://127.0.0.1:8787',
      '/qp-icons': 'http://127.0.0.1:8787',
      '/icon-192.png': 'http://127.0.0.1:8787',
      '/icon-512.png': 'http://127.0.0.1:8787',
      '/apple-touch-icon.png': 'http://127.0.0.1:8787',
    },
  },
})
