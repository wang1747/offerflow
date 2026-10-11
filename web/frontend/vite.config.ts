import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    // 手机版外壳跑在系统 WebView 上。Android 11 出厂的 WebView 是 Chrome 83，
    // 而逻辑赋值运算符（??= / ||= / &&=）要到 Chrome 85 才有 —— 不降级的话，
    // 这些设备打开就是白屏（Uncaught SyntaxError: Unexpected token '='）。
    // Vite 默认 target（chrome87）不会降级它们，所以这里显式钉到 chrome83。
    // 说明：esbuild 只负责语法降级；?. 与 ??（ES2020）Chrome 83 本就支持，会被保留。
    target: ["chrome83"],
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
      },
    },
  },
})
