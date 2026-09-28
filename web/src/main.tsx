import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { installStageUnit } from './lib/stage'
// 旧界面那 1499 行 CSS 原样搬过来（class 名保持不变 → 样式直接生效，一行没改）
import './styles/app.css'

installStageUnit()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
