# macOS 移植说明（本地适配副本）

本目录是 [490720818/qq-pet-copilot](https://github.com/490720818/qq-pet-copilot)（GPL-3.0）的 macOS 本地适配版。
目标运行方式：**控制台调度器 + Android 真机**（不使用 Windows 模拟器链路与 GUI）。

## 相对上游的改动（分支 `mac-port`）

1. `src/emulator.py` — `winreg` 顶层导入改为 try/except 降级（非 Windows 用空实现兜底）。
   原代码在 macOS 上导入即崩；现在模拟器注册表探测安全返回空结果。
2. `src/config.py` — adb 探测加入 macOS 路径（`/opt/homebrew/bin/adb`、`/usr/local/bin/adb`、
   `~/Library/Android/sdk/platform-tools/adb`），支持 `~` 展开；找不到时的报错文案通用化。
3. `src/notify.py` — 新增 `_send_mac_toast`（osascript 桌面通知），复用 `notify.win_toast` 开关；
   Windows Toast 逻辑原样保留。
4. `src/scenario.py` — **新版 QQ（9.3.6x）回主页兼容修复**（真机实测发现并验证）：
   - 现象：打工/学习面板（QPublicFragmentActivity）返回主页时，3D 主页需 1~2 秒重渲染；
     原逻辑按 back 后立即检查、未命中就再按 back，会连锁把宠物模块整个退回关闭，一路退回
     系统桌面，`ensure_main_page` 抛"无法回到主页面"；恢复重试同样失败 → 告警退出调度器。
   - 修复 A：每次 back 后先等 `BACK_SETTLE_SECONDS`(2s) 再检查，避免没等到渲染就连环按退。
   - 修复 B：连续 `SCHEME_FALLBACK_BACKS`(3) 次 back 未回主页时，改用官方 scheme
     （`mqqapi://qpet/open`，零点击、无需 root）重开宠物页并等渲染兜底；最多兜底 2 次。
   - 实测：打工面板→主页 3.3s 成功；从系统桌面（最坏状态）14.3s 自愈成功。
5. `config.yaml` — 本地配置（不入库）：`adb.path=/opt/homebrew/bin/adb`、
   `device_serial=192.168.50.40:5555`（无线 adb，靠 `adb tcpip 5555` / `persist.adb.tcp.port`，
   手机换网或掉 Wi-Fi 后需重新 connect）、`recover.method=重启游戏`
   （真机重启后若设锁屏密码，恢复链会卡解锁，故不用 adb reboot）。
6. `src/u2dev.py` + `src/recover.py` — **无线 adb 掉线自愈补链**
   （2026-09-21 实测：13:45 与 02:48 各一次"u2 截图失败: device offline → 恢复失败 → 告警退出"，
   手机侧完全正常，`adb kill-server` 后一条 `adb connect` 即恢复）：
   - `u2dev._is_conn_error()` 的关键词表原本没有 `offline`，`device offline` 被判成非连接故障
     → `_u2_op` 直接抛出不重连 → `_reconnect() → _wait_device_online() → ensure_connected()`
     （其 `_recover_unreachable` 在"adb 报不可达但内核 TCP 直连目标端口可达"时会自动
     kill-server 修 server 网络栈）从未被触达。现补入 `offline` / `no route to host` / `不在线`，
     以及 `device '<serial>' not found` 形态（序列号夹在中间，匹配不到 `device not found`）。
   - `recover.reenter_pet()` 的"重启游戏"分支（本机 `recover.method`）先
     `adb.ensure_connected()` 再 force-stop，否则 adb 层一掉线就当场判"恢复失败"告警退出。
7. `src/u2dev.py` + `scenarios/runner.py` — **启动路径等待重试 + 顶层崩溃入日志**
   （2026-09-23 实测：09-21 18:24 / 09-22 23:06 / 09-23 00:14 反复"启动不了"，根因都是
   无线真机抖动掉线的那一瞬点了"开始"；对照组 09-22 13:06 那次调度器正在运行，
   靠 45s 轮询自己等到回线）：
   - `U2Device.__init__` 原先直接 `ensure_connected()`（一次性判定），设备恰在那一瞬不在线
     就当场抛 `AdbError` 退出，且退出后无人再重试；现改走 `_wait_device_online()`
     （内部先试一次 `ensure_connected`——含 `_recover_unreachable` 的 kill-server 自愈——
     失败才轮询等 `DEVICE_ONLINE_WAIT`=45s），与"运行中掉线"的自愈能力对齐。
     USB 真机 / `emulator-*` 不含 `:`，仍是立即抛错走既有恢复链路，行为不变。
   - `scenarios/runner.py` 顶层补 `except BaseException`，把 traceback 逐行 `log()` 进
     `runs/logs/<date>.log`（仪表盘"实时日志"读的就是它）；原先 traceback 只落 stderr
     （`runs/runner_console.log`），手机上完全看不到崩溃原因，只能人肉翻文件。
8. `src/config.py` — **「优先雇佣」接入「雇佣好友」的目标列表**
   （2026-09-23：设置页把「优先雇佣」配成喵帕斯～，雇佣好友任务却按 `hire_friend.friend_name`
   去了柠檬..家；根因是该字段在仪表盘设置页**没有入口**——只有 `enabled` 有开关，
   `friend_name` 只能手改 config.yaml，用户找不到）：
   - 新增 `_hire_friend_raw()`：构造 `HireFriendConfig` 时把 `work.hire_name`
     （设置页「优先雇佣」，有入口）排到 `friend_name` 列表**最前**，原值其余项留作备选。
     两者语义本就一致（都支持宠物名/主人名部分匹配），且 `friend_name` 已支持
     逗号/顿号分隔的多备选 + "按序取第一个能找到的"（见 `scenarios/hire_friend.py` 的 `run()`）。
   - 于是"想雇谁"只需在设置页配一次「优先雇佣」，雇佣好友任务自动跟随；
     `hire_name` 留空时行为完全不变（原样用 `friend_name`）。
9. `dashboard.py` — **设置页清理：去混淆、补缺失、修反向开关**
   （2026-09-23 全量盘点：14 张二级分组卡片里，6 个时长配置语义交叉、一个反向开关、
   两处配置没有入口）：
   - **反向开关改正向**：`swSchool` 原标签"只打工不学习"，字段却是 `school_enabled`，
     渲染与收集各取反一次（四重否定）。改为「启用学习」，开关态与收集都正向，
     与「调度」页的「学习」勾选框语义一致。
   - **两张时长卡片按机制重划**（不删配置、调度行为不变——六个字段在
     `runner.py` 里各有独立判定：`_quota_over` 按**各自**时长、`_duration_over` /
     `_work_over` / `_fatigue_skip` 按**合计**）：
     「学习 / 打工 配额」= `study_quota_hours` / `work_quota_hours` + 金币阈值；
     「合计停止点与收益档」= `daily_hour_limit`(停学习) / `work_stop_hours`(停打工) /
     `efficiency_tier2_hours`(全停) / `efficiency_tier1_hours`(降收益)。
     标签由"合计预算 / 打工停 / 第一层门槛 / 第二层门槛"改为直述作用，并在说明里点出
     "前三项任一命中即生效""三者同值时后两项形同虚设"——旧标题"疲劳分两层"把这层
     覆盖关系藏起来了（默认三个 12 全填时完全冗余）。
   - **补缺失入口**：新增「雇佣好友」卡片（`swHF` 开关 + 每天次数；目标好友跟随
     「打工→优先雇佣」，即本节第 8 条）；护理卡片补「检查间隔」`care.interval_seconds`
     （此前只能改 config.yaml）。
   - 后端同步：`editable_snapshot()` 回填与 `apply_settings()` 映射各加
     `care_interval` / `hire_friend_enabled` / `hire_friend_times`；二级菜单 `MENU`
     新增「雇佣好友」项、更新两张时长卡片标题。
10. `src/locators.py` — **选课/选工作选择框 xpath 修复（游戏更新后中间多一层 FL）**
    （2026-09-24：打工任务反复报"未定位到选择框，无法归位"，重试/回主页/重启设备
    三级全挂后告警退出；异常截图显示**页面完全正常**，三张课时卡都在）：
    - 根因：`SELECT_BOX_XPATH` 中间用纯子节点步进 `/FL[1]/FL[1]`，而游戏更新后实际结构
      变成 `RecyclerView[1]/FL[1]/FL[1]/FL[1]/RecyclerView[1]`（**中间多包了一层
      FrameLayout**）→ 整条链断掉，`select_box_1/3` 全部定位失败
      （实测 dump：原 xpath 无匹配；补一层或用 `//` 均命中 `(54,789,1026,1091)`，
      按 2:2:1 切出 248/637/929、y=940，与结构变化前的成功日志完全一致）。
    - **为什么白天正常、晚上才炸**：`select_box_container` 标了 `'cache': True`，而
      `_bounds_cache` 是**模块级**字典 —— 旧进程里早已缓存了结构变化前命中的 bounds，
      此后 `see_bounds` 走缓存直接返回、压根不查控件树；20:25 重启调度器后缓存清空，
      重新查询才暴露。**结构性失效 + 进程级缓存 = 重启才现形**，
      排查时别被"之前一直好好的"误导。
    - 修法：中间那段 `/` 改 `//`（`FL[1]/FL[1]//RecyclerView[1]/FL[1]`），头部两层锚定与
      尾部结构不变 —— 新旧结构都能命中同一容器，也不会被页面下方其它 RecyclerView 抢中。
    - 影响面：该 xpath 由**学习（学园选课）与打工（选工作时长）共用**，
      `work` / `hire_friend` / `school` 三条流程都会中招，不只是打工。

11. `dashboard.py` — **设置页「合计停止点与收益档」合成一个设置项、收益档改为只读说明**
    （用户要求："不要区分学习和打工，就学习和打工加起来满 12 个小时就停止" ＋
    "（收益档）那就不要作为设置项啊，直接告知用户就行了"）：
    - 原来四行（合计满则停学习 `daily_hour_limit` / 合计满则停打工 `work_stop_hours` /
      合计满则全停 `efficiency_tier2_hours` / 降收益门槛 `efficiency_tier1_hours`）里
      三条停止线语义互相覆盖：按默认 12/12/12 填时后两项完全冗余，用户还得先弄懂
      "哪一行管学习、哪一行管打工"才能配对。
    - 现在这张卡只有**一个设置项**：「合计满则停止」（`numStopTotal` → `stop_total_hours`，
      默认 12，0 = 不限）。保存时 `apply_settings` 把 `schedule.daily_hour_limit` /
      `work_stop_hours` / `efficiency_tier2_hours` **三键一起写成同一个值**
      （`stop_total_keys`）——引擎不动（三处判定原样保留：`scenarios/runner.py` 的
      `_duration_over`/`_work_over`/`_fatigue_tier`），调度器不用重启、配置热加载照旧；
      老配置三项不一致时由 `stop_total_hours()` 折算显示值，表单里给 ⚠ 提示
      （动一下这个数保存即统一）。
    - **收益档不再作为设置项**（用户要求："那就不要作为设置项啊，直接告知用户就行了"，
      紧接着又嫌"当前设置 / 收益档 / 说明"三段长文啰嗦 → "你就弄用户看得懂的，
      弄个表格放在下面"）：`efficiency_tier1_hours` / `efficiency_tier2_hours` 从表单里
      拿掉输入框，也不写长段落，改成输入框**下面一张 4 行小表**（`stopExplain()` 渲染
      `.mintbl`：什么时候 → 会发生什么）：
      没到 8 小时 → 正常收益 100%；满 8 小时 → 收益降到 25%（只提示，继续跑）；
      满 N 小时 → 学习和打工一起停，转冒险；次日 0 点 → 时长清零，重新开始。
      数字随 `editable_snapshot()` 动态生成（手改 config.yaml 会跟着变）；
      `saveSettings` 不再收集 `#numEffT1`（`apply_settings` 映射保留作旧客户端兼容）；
      底层三项停止线真不一致时才额外多一行 ⚠（`stopWarn()`，平时不占地方）。
    - 顺手同步：主页胶囊分母、状态页汇总行（两条 → 一条「合计停止点」）、
      「一键预设」按钮（改写 `#numStopTotal`）、`quotaHint` 提示
      （`stopHint` 那行已随三段长文一起删掉，改由表格体现）、
      `config.example.yaml` 与 `src/config.py`/`src/settings.py` 的默认值与注释
      （新装默认 12 = 合计满 12h 两项一起停；收益档两键标注"不是设置项"）、
      README/AGENTS.md 对应说明。
    - 三项分开设的玩法（如 8/12/12 = "学满 8h 后继续吃 25% 档打工到 12h"）仍可手改
      config.yaml，引擎照旧认；只是仪表盘不再暴露这两个数。
12. `dashboard.py` + `scenarios/care.py` — **资料卡状态环改成官方的三层同心环 + 点开看数值**
    （用户指出："官方的状态环是三个圈，内层/中层/外层，最外层是体力、中层是清洁、内层是心情；
    那个圈还可以点开，点开之后能展示具体的体力/清洁/心情"）：
    - 原实现是一条 `conic-gradient` 固定比例三色块（绿 70% / 橙 18% / 蓝 12%）+ 中心镂空：
      既不是三个圈、比例也写死，而且"调度器没跑/页面断连"时整圈涂红 —— 与官方含义冲突。
      官方实拍对照：`qqpet_assets/01_ui_screenshots/02_pet_home.png`（收起态，
      外蓝/中绿/内橙三层弧、未满处是灰弧）、`05_pet_status.png`（点开态：
      体力 82 / 清洁 90 / 心情 98 三行 + 各自进度条）。
    - 现在 `.idring` 是 **SVG 三层圆弧**：外=体力(`--energy` 蓝)、中=清洁(`--clean` 绿)、
      内=心情(`--mood` 橙)，各圈按 0~100 画弧（`stroke-dasharray/dashoffset`），
      未满那段是灰底 `.trk`；数据取 `/api/data` 的 `status`（= `runs/status_cache.json`），
      三个都没读到 = 三圈全灰。**灰底别调太淡**（试过 `rgba(...,.18)`，米白卡上缺口完全看不见）。
    - 点环 = **胶囊原位向下展开**（官方交互，见下条），不是底部弹窗。
    - 环不再兼任"调度器在跑"的指示灯：那个信息由左侧头像绿/红描边 + "运行中/未运行"文案承担。
13. `dashboard.py` + `static/qp-icons/official/status_*.png` — **状态环点开改成官方那套"原位展开"**
    （用户："官方的做法是直接在胶囊下面显示，有一种展开的感觉……你先去实际游戏画面里
    点击那个三个圈看看是什么操作"）：
    - **真机实测（2026-09-28 01:44，一加 LE2120 / 1080×2412）**：先 `touch runs/pause.signal`
      让调度器让路（`wait_if_paused`，安全点原地等待、不丢 pending），再
      `adb shell input tap <状态环中心>` → 卡片**原位向下长成圆角面板**：名字行不动，
      下面出现 体力/清洁/心情 三行（图标 + 名称 + 数值 + 进度条 + 右侧 `›`），
      底部中央一个 `^` 收起箭头；**全程没有遮罩/弹窗，页面其它部分照常可见**。
      点某行的 `›` 会进游戏自己的详情页（实测点中 心情行 → 弹出「心情值」页，
      里面有抚摸/喂食/洗澡/首次回家/冒险 + 跳转按钮）——本工具没有那些页面，
      所以 `›` 只作视觉还原（CSS `pointer-events:none`，不做点了没反应的假按钮）。
      测完 `rm runs/pause.signal` 交还控制。
    - **量尺寸的基准（踩过大坑）**：页面的 `--u = clientWidth / 360`（JS 写 `--vu`），
      所以量官方图必须 **÷3**（1080 原生 → 360 CSS px = u）。第一版误按 ÷2.769
      （当成 390 宽）量，整体大了 8%：字号 14u→应 12u、行距 41u→应 37.5u、
      条厚 5u→应 4u、图标框 18u→应 17u、`›` 16u→应 11u，用户直接看出来"比官方大很多"。
      现在全部按 `qqpet_assets/01_ui_screenshots/05_pet_status.png`（1080×2412，÷3）实测：
      卡片 74.0..280.7 × 34.3..214.0（**207 × 179.7u**，和收起态同宽）、
      三行中心 y=99.4 / 136.9 / 175.5（**行距 37.5u**，头部与首行之间有 3.3u 间隙）、
      图标框 82.5..99.5（**17u**）、标签起点 100.3、数值起点 130.7（字 **12u**）、
      进度条 x 159..250.7（**轨道 91.7u，厚 4u**）、`›` x 261..265.7（**11u 字号**）、
      **数值/标签字重都是常规 400**（官方数值墨宽 12px、墨水密度 0.44；写 680 会变 15px/0.51
      —— 比官方粗且宽、还把进度条往右挤，用户实报；用 `min-width:20u` 锁住数值列宽，
      "92→100" 切换不会挤到进度条）、
      `^` 8.4×4.3u 居中在 17u 高的箭头区里。行区展开高度 = 3×37.5 + 17 = **133u**（含 3.3u 间隙）。
    - **状态环的三圈粗细**（用户实报"那 3 个圈细了挺多"）：按官方原图做**径向剖面**实测，
      官方三圈带宽 2.6~2.9u、中径 4.75 / 8.70 / 12.50u（包围盒直径 27u）。SVG `viewBox 40`
      渲染成 27u ⇒ **1u = 1.4815 vb**，所以 `stroke-width` 要 3.3 vb（≈2.2u 几何 + 抗锯齿 ≈2.8u），
      半径取 7.05 / 12.5 / 18.1 vb（= 4.76 / 8.44 / 12.2u）。原来写 `stroke-width:2.6`
      （=1.76u）三圈明显偏细、而且整体偏内。改后实测：2.9 / 2.6 / 2.4u、中径 4.65 / 8.40 / 12.20u。
      我们用 `getBoundingClientRect` 核过：卡片 207×179、图标框 82.5..99.5、标签 100.3、
      数值 130.3、条 158.3..251.3 厚 4、`›` 261.3、行中心 99.3/136.8/174.3 —— 与官方逐项对齐
      （对比图 `runs/card_vs_official.png`：左官方右我们，同一 u 尺度并排）。
    - **"更白一档"的白玻璃 token `--panel-bg-strong: rgba(255,255,255,.80)`**：
      用户先要求"把 QQ宠物托管这个胶囊的背景透明度调低一点，让它更白一点点"，
      接着说"左边的 4 个按钮的背景跟它一样" → 新增这个 token，**资料卡 `.idcard` 与
      左列 4 个功能圆钮 `.rbtn` 共用同一份**（改一处两边同步，不会再各调各的）；
      任务卡 `.qpanel` / 底栏 `.deck` / 胶囊仍走 `--panel-bg(.62)`，右列圆钮仍是
      `--btn-r-bg` 黑蒙版。实测：资料卡底色 (249.4,241.4,224.3) → (252.1,247.9,238.8)，
      左圆钮 (215.7,176.4,138.5) → (226.6,195.4,167.8)（含图标像素）。展开态沿用同一背景。
    - **卡片宽度不改**（用户要求"展开之后右边会被撑大一点，能不能不要改宽度"）：
      收起/展开都是胶囊的 207u（官方本来也不变宽，之前那个 224u 是我算错尺度得出的假数）。
    - 三行图标 = **从官方原图上紧裁的图标**（`status_energy.png` 蓝闪电 /
      `status_clean.png` 绿水滴 / `status_mood.png` 红心）：按饱和度算 alpha 抠掉米白卡底，
      裁到字形外 1.5u 边距，页面里用 17u 见方 + `object-fit:contain` 还原官方大小。
    - 四条踩过的坑（都来自用户实报，改完用 `getBoundingClientRect` 量过，别只靠肉眼）：
      ① **错位**：卡片高度写死 46u 又把 `.idtop` 设成 46u（+6u padding = 52u）撑破胶囊 →
         现在高度由内容撑、`.idtop = --h-card - 6 = 40u`，头像/环离卡片左/上各 3u、右 8u、垂直居中；
      ② **动画**：别用 `display:none` 硬切（卡片瞬间跳高）→ 行区 `height:0 → 139u` 过渡，
         `^` 收起箭头放进行区一起长出来；
      ③ **圆角**：别自造小圆角 → 沿用胶囊圆角（`--h-card / 2` = 23u），两态同一个值；
      ④ **背景**：展开态**不要覆盖背景**，就是收起时那个材质（`--panel-bg` + `--panel-blur`），
         一度写成 `rgba(249,239,222,.94)` 淡黄被用户指出。
    - 旧的 `#petSheet` 底部面板（含 `.pmask/.ppanel/.pitem` 那套 CSS）整段删除。
    - **让环/展开行真的有数**：`scenarios/care.py` 新增 `refresh_status_cache()`，
      `check_and_care()` 的"一键护理"分支点完补读一次状态面板（白嫖一屏 OCR，读到体力就直接用；
      否则 toggle_status 展开 → `read_status_ready` → 收起），把 `one_click_care` 清掉的
      体力/清洁/心情写回缓存 —— 否则一键护理模式下这三个值每轮都被清空，环永远是灰的。
      补读失败只记日志，绝不影响护理判断。**care.py 的改动要重启调度器才生效**
      （实测重启后每 90 秒一条 `状态缓存刷新: 体力=92 清洁=96 心情=100`）。
    - 验证：探针页（`.probe_*.py`，验完删除）数据到手后 `togglePetCard(true)` 截图，
      与真机展开态逐项对得上；JS 语法用 `/System/Library/.../jsc` 过一遍（无 SyntaxError）。

14. `dashboard.py` — **任务列表（总览页任务队列卡）状态层级重做**
    （用户："感觉任务列表的排版和样式有点丑了，能不能帮我看看怎么改的更精致一些"）：
    - 三处硬伤：① 禁用/完成行被划**删除线**（压在毛玻璃上又脏又像报错）；
      ② `rowOf()` 里算好的右侧状态文字 `det`（执行中/等待·时间/今日完成/今日结束/已禁用）
      是**死代码**——根本没输出，每行只剩"名字 + 勾选框"，没有信息层级；
      ③ 执行中的任务只染了名字颜色，9 行里看不出来。
    - 改法：状态文字接回行右侧（**只写有信息量的**：执行中/等待 HH:MM/今日完成/今日结束/
      已禁用，正常"可执行"留空）；执行中行加淡橙底 `rgba(255,153,15,.14)` + 橙字加粗；
      去掉所有删除线，完成/结束/禁用改靠 `opacity` + 灰字分层；行距内距从写死 px 改成 u；
      分隔线 `.13 → .11`、两端 8%→92% 淡出。**不加图标**（第十四轮明确删过）。
    - **同轮第二件事（用户："这些东西离那个圆角的卡片边框就感觉不是很适配"）**：
      ① `.qpanel` 圆角 22u 但内边距只有 `8u 9u` → 内容贴着圆角，改成 `14u 16u 12u`
      （≈ 圆角的 0.6~0.7 倍）；② `@media(max-width:639px)` 的"窄屏收紧"分支在手机上
      **永远生效**，里面把任务行覆盖成写死的 px（行内距左右 0、勾选框 18px、字号 13px），
      跟 `--u` 那套尺寸打架 —— 现在尺寸全交给 `--u`，该分支只留 `.ttag{display:none}`。
      改后标题左缘与行名左缘对齐（都是 100.0u，改前 83 vs 89）。
    - 细节与"怎么再微调"写在 `qqpet_assets/web/INTEGRATION.md` 第十一节（第十九轮），
      对比图 `runs/tasklist_compare.png`（状态层级）与 `runs/tasklist_layout_compare.png`（排版）。

15. `scenarios/runner.py` + `dashboard.py` — **延时收尾期间"任务行不亮"修复**
    （用户："现在是雇佣打工中，但是为什么雇佣好友没有被选中显示运行中"）：
    - 根因：主任务非阻塞延时收尾时 `queue_status.json` 的 **`current` 是空的**
      （调度器已回主页跑支线），活动只在 `pending: '雇佣打工'` 这个中文描述里；
      仪表盘行高亮只认 `current` ⇒ 那行永远不亮，只有标题右侧提示条在说话。
    - 改法：runner 新增机器可读的 **`pending_key`**（`_main_pending_key()`，写 school/work/
      hire_friend/adventure 键）；前端 `pendKey = q.pending_key || 描述兜底映射`，
      命中行橙色高亮 + `▶ 进行中`（`current` 驱动的是 `▶ 执行中`，两者语义不同）。
      前端有兜底映射 ⇒ **不用等调度器重启**就生效；`pending_key` 在调度器下次重启后生效。
    - 细节见 `qqpet_assets/web/INTEGRATION.md` 第十二节（第二十轮），
      截图 `runs/tasklist_pending.png`。

16. `dashboard.py` — **任务行显示"对象"**（用户："好友护理后面能显示护理的谁吗"）：
    - 好友护理行跟一个浅色小字显示护理对象（`friend_care.friend_name`），
      雇佣好友行同理显示雇佣目标（优先 `work.hire_name`，兜底 `hire_friend.friend_name`）；
      数据来自 `editable_snapshot()`，改配置下一轮 `/api/data` 就变。
    - 关键约束：任务行可用宽度只有 **155u**，"名字+对象+状态+勾选框"塞不下 →
      ① 删掉与行内 `gap` 重复的 margin（`.msub`/`.mcb` 白吃 14u，正是"喵帕斯～"被截成
      "喵帕…"、雇佣好友那行对象名被挤没的原因）；② 带对象的行把状态压成极简
      （`▶` / `12:00` / `✓` / `—`）；禁用行不显示对象。
    - 对象名与状态文字包进贴右的 `.mright` 簇（auto 边距只挂簇上），与其它行的
      状态文字**右缘对齐**（用户："怎么没有跟其他的一样右对齐呢"）——实测 9 行右缘都是 227.0u。
    - 选中/进行中的 `▶` 从右侧那一簇挪到**任务名后面**（用户："那个被选中的箭头放到
      任务名的后面去"），右侧只留对象名/状态文字；行宽核过刚好放得下。
    - 细节见 `qqpet_assets/web/INTEGRATION.md` 第十三节（第二十一轮），
      截图 `runs/tasklist_sub2.png` / `runs/tasklist_right.png` / `runs/tasklist_arrow.png`。

17. `scenarios/runner.py` + `dashboard.py` — **"福袋什么状态都没有"背后的两个真 bug**
    （用户："福袋是个什么逻辑 为什么什么状态都没有"）：
    - **① 场景自己的时间段没算进节流**：`_scen_throttle_next()` 原来只算 `last + interval`，
      而队列级 `_eligible` 只看 `tasks.*.enabled_time_range`（默认空=不限）——
      于是凌晨 3 点、窗口 08:00-23:59 的福袋被打成 `ready`（可执行），
      而行里"可执行"是不写字的 ⇒ 整行空白。现在把场景 `time_range` 并进节流：
      窗口外返回下一个开始时间（跨零点窗口支持），行里显示 `等待 08:00`。
    - **② `pending_key` 认错键**：启动实测是**借道 school 场景**做的检测
      （`scen = self.school`），"正在打工"的 pending 却挂在 school 上 →
      `_main_pending_key()` 按场景归属写成 `school`，仪表盘去高亮「学习」。
      改成**按 pending 描述认键**（上课/打工/雇佣打工/冒险），场景归属兜底；
      前端也改成描述优先、`pending_key` 兜底。
    - 附带：行内距 10u→8u、gap 8u→6u、勾选框 20u→19u，给"对象名+状态"腾 ~11u
      （好友护理行从"喵…"变成"喵帕… + 08:00"），`.msub` 加 `min-width:36u` 下限。
    - 细节见 `qqpet_assets/web/INTEGRATION.md` 第十四节（第二十二轮），
      截图 `runs/tasklist_states2.png`。

18. 好友护理 / 福袋 **时间段默认改全天 + 补进设置页**（用户："好友护理和福袋的时间段设置项里没有啊
    改成默认全天"）：
    - 默认值：`friend_care.time_range` 14:00-19:30 → **00:00-00:00**、
      `gift_bag.time_range` 08:00-23:59 → **00:00-00:00**（`src/config.py` 两个 dataclass +
      在线 config.yaml；`config.example.yaml` 只有 friend_care 段、没有 gift_bag 段）。
      语义：`in_time_range()` 里 `end <= start` 视为跨零点 ⇒ **起止相同 = 全天**
      （写 08:00-23:59 反而会漏掉 23:59 那一分钟）。
    - 设置页补字段（仪表盘原来一个 time_range 都没有）：快照 `friend_care_range` /
      `gift_bag_range` → 映射 `friend_care.time_range` / `gift_bag.time_range`，
      两张卡各加一行文本输入；`src/settings.py` 的 DEFAULTS 补
      `friend_care.time_range`（原有那条改成 00:00-00:00）与 `gift_bag.time_range`，
      `validate_field` 把 gift_bag 并进原有的时间段校验分支（HH:MM-HH:MM、带引号写回）。
    - 实测：设置页显示 `00:00-00:00`；`POST /api/settings` 合法值 applied、
      `abc` 被拒（`rejected: ["gift_bag_range: 非法值 'abc'"]`）；config.yaml 落盘。
      调度器**热加载**（`friend_care.cfg.friend_care` / `gift_bag.cfg.gift_bag` 都是整体替换）
      ⇒ 不用重启，队列状态立刻从"等待 08:00"变成按间隔的下一次
      （实测 好友护理 等待 10:47、福袋 等待 11:06 = 上次跑完 + 各自 interval）。

19. `dashboard.py` — **相邻的两条任务高亮不再糊成一片**（用户："两个选中的 因为中间没有
    间隙会连在一起"）：
    - 场景：宠物在上课（pending=上课 →「学习」行 `▶ 进行中`）+ 调度器同时跑好友护理
      （current=好友护理 → 该行 `▶ 执行中`），两行相邻、底色贴在一起。
    - 改法（三次返工后的最终版）：**高亮药丸由 `.mrow.run::before` 伪元素画**
      （`position:absolute; z-index:-1; left/right:0; border-radius:8u`），
      行本身不动盒模型 ⇒ 行高与普通行一致（实测都 35.23u）。
      **顶部是 `top: calc(2u + 1px)`、底部 `bottom: 2u`** —— 分隔线占掉行顶 1px，
      不让的话手机 u=1 时"上缝 1px / 下缝 2px"（用户第四次实报"上下还是不对称"）。
      定量验收（线染蓝/药丸染红，DPR3）：u=1 时上缝 2.33px、下缝 2.33px、差 0.00。
      **两条弯路别再回去**：① `background-size/position` 内缩会被元素自身的 border-radius
      裁切，药丸圆角撞上"整行"的圆角弧（用户："被选中的圆角感觉很奇怪"）；
      ② 透明边框 + `background-clip:padding-box` 形状对了，但 `calc(var(--u)*2)` 的边框
      被浏览器取整成 2px（应 2.78px），高亮行矮 1.1u。
    - 细节见 `qqpet_assets/web/INTEGRATION.md` 第十六节（第二十四轮），
      截图 `runs/tasklist_highlights.png` / `runs/tasklist_dividers.png`。

20. `dashboard.py` — **右侧功能栏"偶尔上下抽动"**（用户："右侧的启动 停止 画面 这两个组件
    为什么偶尔还是会上下抽动"）：`.funcbar` 的 top 由 JS `place()` 按"栏底 = 列表底"算，
    挂在初始化 / `resize` / `MutationObserver(#taskList)` 上 ⇒ 列表每重建一次就重量一次。
    抖动源：① 量在 6 秒刷新重建行的中间态；② 列表底部亚像素浮动；③ 手机 `resize`（地址栏）
    与 bfcache `pageshow`。改法：`placeSoon()` = 立即量 + 下一帧补量 + 150ms 超时兜底
    （只靠 rAF 在无头/后台会不触发），并加 **0.4u 迟滞**（差 <0.4u 不写 top）；
    实测：删一行 top 288.7→253.4（跟随 ✓）、padding 微调 0.2px 不动 ✓、
    180 秒 1500 次采样范围 0.00u。细节见 `qqpet_assets/web/INTEGRATION.md` 第十七节。

未涉及：模拟器管理（winreg 探测在 Mac 自然失效）、`src/opener.py` 的模拟器门禁注入路径（真机不用）、
`main.py` GUI（win32 + scrcpy 嵌入，Windows 专用）。

## 环境与运行

- Python 3.13（`.venv/` 已建好，依赖已装：uiautomator2 / rapidocr / onnxruntime / Pillow / onepush / ruamel.yaml / PyYAML）
- 真机准备：USB 调试打开；QQ 已登录且 ≥ 9.3.25；充电时不熄屏
  （`adb shell settings put global stay_on_while_plugged_in 7`）
- 启动：`./run.sh`（= `.venv/bin/python scenarios/runner.py`），Ctrl+C 停止
- 单测：`./run.sh --test coins` 等
- 手机仪表盘：`./dashboard.sh`（纯标准库零依赖，默认端口 8787，绑定 0.0.0.0）
  内网访问 `http://<Mac 的局域网 IP>:8787`，手机浏览器打开即用；
  数据源为 runs/ 下的日志/进度/状态/队列文件，日志 3s、数据 6s 自动刷新。
  **后台常驻**：脚本用 `start_new_session` 脱离终端启动（关终端不影响），
  已在运行时提示 PID 不再重复启动；换端口 `./dashboard.sh --port 8888`；
  停止 `kill $(lsof -nP -iTCP:8787 -sTCP:LISTEN -t)`；日志 runs/dashboard_console.log

## 真机实测记录（一加 LE2120 / Android 14 / QQ 9.3.60）

- u2 连接、截图、整屏 OCR、金币识别（1600）、主页面判定均通过
- 已实际跑通：护理检查、好友踩踩（10/10）、帮好友照顾（经验日常）、PK（15/15，含卡局自愈）、
  打工开始 + 鼓励宠物 10 连击
- 告警链路：macOS 通知 + 截图存档实测生效（`runs/alert_*.png`）
- 待观察：学习（school）流程、打工结算收取、被雇佣、长时间连续运行的稳定性

## 远端与上游同步

- `origin` = 自己的 fork：<https://github.com/mps233/qq-pet-copilot>
  （`git push origin mac-port`）
- `upstream` = 上游：<https://github.com/490720818/qq-pet-copilot>

本地适配改动已并进 `main`（fork 默认分支即为带改动的版本）。
同步上游：`git fetch upstream && git merge upstream/main`，
冲突预计只落在改动过的源文件。
