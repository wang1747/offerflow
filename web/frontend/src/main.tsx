import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { initTheme } from './utils/theme'
import { initOffline } from './offline'

initTheme()

// 离线层：缓存读接口、离线写排队、联网自动同步。
// Service Worker 在 HTTP（IP + 非 HTTPS）下注册不上，所以缓存与队列都由应用层自己管理。
initOffline()

// 注册 Service Worker：让「添加到主屏幕」后具备独立窗口与离线外壳能力。
// 只在生产环境注册 —— 开发环境注册会干扰 HMR（缓存住旧模块）。
// 注意：这在 HTTP 站点上必然失败（非安全上下文），失败是预期的，上面 initOffline 才是主力。
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* 注册失败不影响主流程（如非 HTTPS 环境） */
    })
  })
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
