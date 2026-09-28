# 前端（React 版 web/）踩坑清单

**动手改 `web/` 之前先读这份。** 每一条都对应一次真实事故，写明"改了 X 就必须验 Y"，
而不是泛泛的"要小心"。

---

## 一、改完必须验证的三件事

改任何涉及**高度 / 定位 / overflow / 容器层级**的 CSS 或 JSX，提交前必须跑探针量数值。

| 改了什么 | 必须量 | 期望 |
| --- | --- | --- |
| `height` / `min-height` / `position` | `.home` 的高度 | ≈ 视口高 |
| `overflow` | `.home` 的 `scrollHeight - clientHeight` | 0（该页本来就不该滚时） |
| 包了一层容器 | `grep -n "main >"` 逐条确认还匹配 | 无失效 |

**为什么 `.home` 的高度这么脆**：`.home` 里全是绝对定位的浮动层（`.flt`），
它**撑不开任何父级**；而 `min-height:100%` 要求**整条祖先链都有确定高度**。
链条断在任何一层 → 百分比失效 → 整页塌成只剩背景。

**这个错犯过 3 次**，断点分别在 `.app`、`main`、`.page-slot`。当时的层级是：

```
body → #root(display:contents) → .app → main → .page-slot → section[data-page]
```

**给 `.home` 或其祖先加 `position:relative` 也会踩这个** —— 一旦某一层变成"由内容撑高"，
链条就断了（实测：加上 relative 之后立刻只剩背景）。

---

## 二、测量环境：headless 测不准什么

### 最小视口 500px（犯过 2 次）

**headless Chrome 的最小视口宽是 500px**。`--window-size=360` 截出的 PNG 是 360 宽，
但页面**仍按 500 排版后裁掉右侧** —— 直接看会误判成"窄屏正常"。

**必须用同源 iframe 承载**：

```html
<!-- 存成 web/dist/assets/_shot.html（放 / 下会 404：路由只服务 index.html 与 /assets/*） -->
<!doctype html><html><body style="margin:0;background:#888">
<iframe src="/?tab=adv" width="360" height="1400" style="border:0;display:block"></iframe>
</body></html>
```
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --window-size=380,1420 --hide-scrollbars --virtual-time-budget=18000 \
  --screenshot=/tmp/x.png "http://127.0.0.1:8787/assets/_shot.html"
```

### 完全测不出的东西（只能靠真机）

- `env(safe-area-inset-*)` —— headless 里**恒为 0**
- iOS 的手势判定（`pointercancel`）
- 惯性滚动与弹性回弹

**→ 用户说这类问题"有"的时候，直接要真机截图，不要在自己的环境里换假设。**
（犯过：绕了四轮才定位到 `.drawer` 溢出，用户给一张截图就清楚了。）

### 探针的输出必须单行

`JSON.stringify(x, null, 1)` 是多行 → `grep -oE 'data-probe="[^"]*"'` 抓不到。**别加缩进参数。**

---

## 三、iOS 手势 × 滚动容器（反复出现的模式）

**只要交互元素处在滚动容器里**（`.page-slot` / `.home` / `.advlist` / `#logbox` 都是），
iOS 会先判定"这是滚动还是交互"，判定成滚动就发 `pointercancel` —— **原来的手势永远收不到后续**。

| 现象 | 正确做法 |
| --- | --- |
| 圆钮点不开面板 | 用 **`onClick`**，不要 `onPointerUp` |
| 拖拽完全没反应 | 用 **`TouchSensor` + `MouseSensor`**，**不要 `PointerSensor`**（dnd-kit 文档里写着） |
| 拖拽不动（更隐蔽的一种） | `touch-action` 必须**静态**声明；等 `onDragStart` 再加 class 已经晚了——浏览器在**触摸开始那一刻**就读了它 |

---

## 四、选择器与特异性

### 直系子选择器会被"多包一层"整条打断

`main > section[data-page]:not([data-page="main"])` 这类规则，一旦中间插入 `.page-slot`，
**整条失效** → 样式静默回退（这次表现为内页背景从浅灰变米黄、内容贴边）。

**改结构前 `grep -n "main >"`，改后逐条确认。**

### `:not(...)` 的参数也计入特异性

`main section[data-page]:not([data-page="main"])` 是 **(0,2,2)**，不是 (0,1,2)。
想覆盖它得凑到同级别（加个 `.card` 之类），靠"同特异性时后写的胜出"。

### 改 CSS 前先 grep 有没有 `@media` 里的同名规则

`.advlist .ai` 有两份：基规则 126px + `@media(max-width:360px)` 里 96px。
只改基规则 → 真机（≤360 宽）没变化。**用户手机命中的是窄屏分支。**

---

## 五、迁移 legacy 时最容易漏的：JS 副作用

**DOM 里看不出来的东西，搬页面时必须专门清点。** 已漏过并已补的：

- **历史栈**（`pushState` / `popstate`）→ 侧滑返回失效
- **`syncThemeColor()`** → 内页状态栏颜色不对
- **`html[data-page]` 属性** → 内页背景不切白
- **`place()`** → 功能栏与任务列表重叠
- **勾选框的 click 委托** → 点了没反应
- **拖拽守卫**（`e.target.closest('.mcb')` 时不启动拖拽）→ 点勾选框变成拖拽起手

**方法**：搬一个页面时，除了搬 DOM，必须
`grep -nE "addEventListener|setAttribute|\.style\.|dataset\." web/legacy/app.js`
逐条问"这个我搬了吗"。

---

## 六、临时探针的纪律

**不要用 `cp 文件 /tmp/x.bak` 再 cp 回来。** 备份随时可能是**已被污染的那一份**
（真实事故：探针被误提交进 `main.tsx`，页面每 2.6 秒自动弹一次背景面板）。

用 `git checkout -- <file>` 恢复；或者探针写进独立文件注入。

---

## 七、交付前的最低检查

```bash
cd web
node node_modules/typescript/bin/tsc --noEmit     # 类型（用用户自装的 node）
node node_modules/vite/bin/vite.js build
# 布局类改动再加：探针量 homeH / homeExtra
```

**布局类改动没验证就不许提交。** 犯过：先提交再验证，用户看到整页空白。

---

## 八、沟通纪律（这条是给 AI 的）

- **别一次只修一层就交验。** 用户抱怨的是"改了七八轮还在犯同样的错"——
  该做的是把**整条链路**（如"iOS 手势在滚动容器里的完整行为"）一次走通再交。
- **探针说"没问题"但用户说"有"，立刻要截图**，不要继续在测不准的环境里换假设。
- **改用户的真实数据前先确认**（如点击勾选框会写回 `tasks.<k>_enabled`）——
  这类验证要么用幂等的同值，要么留给用户做。
