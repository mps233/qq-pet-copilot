const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const TASKNAME={care:'护理',school:'学习',friend_care:'好友护理',gift_bag:'福袋',hire_friend:'雇佣好友',adventure:'冒险',visit:'踩踩',pk:'PK',work:'打工'};
// 任务类型标签：循环=按间隔反复巡检；每日=每天定时一轮；主线=主任务组
// （提到模块作用域：refreshData 的行渲染和首屏骨架 renderTaskSkeleton 都要用）
const TTAG={care:'循环',friend_care:'循环',gift_bag:'循环',
            visit:'每日',pk:'每日',
            adventure:'主线',school:'主线',work:'主线',hire_friend:'主线'};

// ---- 任务行拖动排序（只对「任务顺序」组生效，日常轮巡固定不动）----
// 用 pointer 事件而不是 HTML5 drag&drop：iOS Safari 的触摸不触发 dragstart。
// 长按 220ms 才进入拖拽 —— 否则会和"点勾选框切启用""上下滑页面"抢手势。
function setupTaskDrag(){
  const list=document.getElementById('taskList');
  if(!list||list.__dragBound) return;      // 每次渲染都会调用，只绑一次
  list.__dragBound=true;
  let st=null;
  const draggable=k=>TTAG[k]&&TTAG[k]!=='循环';
  list.addEventListener('pointerdown',e=>{
    const row=e.target.closest('.mrow[data-k]');
    if(!row||e.target.closest('.mcb')) return;      // 勾选框：交给原来的点击逻辑
    if(!draggable(row.dataset.k)) return;
    st={row:row,k:row.dataset.k,y0:e.clientY,dy:0,moved:false,armed:false};
    st.timer=setTimeout(()=>{
      if(!st) return;
      st.armed=true;
      st.row.classList.add('dragging');
      list.classList.add('dragging-list');
      try{ if(navigator.vibrate) navigator.vibrate(12); }catch(_){}
    },220);
  });
  list.addEventListener('pointermove',e=>{
    if(!st) return;
    if(!st.armed){                                  // 长按没到就滑走了 → 当成滚动
      if(Math.abs(e.clientY-st.y0)>10){ clearTimeout(st.timer); st=null; }
      return;
    }
    st.dy=e.clientY-st.y0;
    st.moved=true;
    st.row.style.transform='translateY('+st.dy+'px)';
    if(e.cancelable) e.preventDefault();
  },{passive:false});
  const end=()=>{
    if(!st) return;
    clearTimeout(st.timer);
    const s=st; st=null;
    list.classList.remove('dragging-list');
    s.row.classList.remove('dragging');
    s.row.style.transform='';
    if(!s.armed||!s.moved) return;
    // 落点：跟其它「任务顺序」行的中心比高度，算出插到第几位
    const others=[...list.querySelectorAll('.mrow[data-k]')]
      .filter(r=>draggable(r.dataset.k)&&r!==s.row);
    const cy=s.row.getBoundingClientRect().top+s.dy+s.row.offsetHeight/2;
    const seq=others.map(r=>r.dataset.k);
    let idx=seq.length;
    for(let i=0;i<others.length;i++){
      const b=others[i].getBoundingClientRect();
      if(cy<b.top+b.height/2){ idx=i; break; }
    }
    seq.splice(idx,0,s.k);
    submitTaskOrder(seq);
  };
  list.addEventListener('pointerup',end);
  list.addEventListener('pointercancel',end);
}

// 拖完写回配置：tasks.order（全量，轮巡组保持在最前）+ tasks.main_order
// （四个主任务按新相对顺序，保证"拖动真的影响调度优先级"，而不只是列表好看）
async function submitTaskOrder(restOrder){
  const loop=Object.keys(TASKNAME).filter(k=>TTAG[k]==='循环');
  const full=loop.concat(restOrder.filter(k=>loop.indexOf(k)<0));
  const mains=restOrder.filter(k=>TTAG[k]==='主线');
  try{
    const r=await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({updates:{'task_order':full.join('>'),
                                    'main_order':mains.join('>')}})});
    const d=await r.json();
    if(d&&d.ok){ taskOrderToast('任务顺序已保存 · 下一轮调度生效'); refreshData(); }
    else { taskOrderToast('保存失败：'+((d&&d.rejected)||[]).join('、')); }
  }catch(err){ taskOrderToast('保存失败：'+err); }
}

function taskOrderToast(msg){
  let el=document.getElementById('torderToast');
  if(!el){ el=document.createElement('div'); el.id='torderToast'; document.body.appendChild(el); }
  el.textContent=msg;
  el.classList.add('on');
  clearTimeout(taskOrderToast._t);
  taskOrderToast._t=setTimeout(()=>el.classList.remove('on'),2200);
}
// 首屏骨架：/api/state 还没回来时 #taskList 是空的，卡片只有标题那么高 →
// 下面「功能栏与任务列表底部对齐」的 place() 会量到一个偏上的"列表底部"，
// 命中 top<100 兜底把功能栏放到 319.3dp（低位），等数据渲染完再跳到真位置
// —— 用户看到的"启动/停止/画面先在下面、过一会儿才跟列表底对齐"就是这个。
// 这里按静态 TASKNAME 先铺**同结构**的占位行（行高只由 .mrow/.mname/.ttag/.mcb
// 决定，跟状态文字无关），卡片高度首屏即到位，place() 一次就对。
function renderTaskSkeleton(){
  const tl=document.getElementById('taskList');
  if(!tl||tl.children.length) return false;
  tl.innerHTML=Object.keys(TASKNAME).map(k=>{
    const tag=TTAG[k]?('<span class="ttag">'+TTAG[k]+'</span>'):'';
    return '<div class="mrow">'
      +'<span class="mname">'+(TASKNAME[k]||k)+'</span>'+tag
      +'<span class="mcb on" data-k="'+k+'"></span></div>';
  }).join('');
  return true;
}
// 任务行的图标映射（TASKICON）随「删除任务名前的图标」一并移除，见 rowOf()。

// 底部状态卡的图标：跟着「当前在干什么」换（冒险/学习/打工各一个图标）。
// 图标全部取自官方素材库，两种来源别混：
//   /qp-icons/<name>-48@2x.png     —— colored/ 那套（书本/香皂/金币/爪印）
//   /qp-icons/official/<name>.png  —— 首页胶囊/右栏那套（爪印金币、PK 字样、指南针）
// 键 = 状态名，两个来源互补：work_eta.kind（上课/打工/冒险/雇佣打工，主任务延时收尾期间
// 最准、带倒计时）优先；没有它时用 queue.current（护理/好友护理/踩踩/PK/福袋等场景
// 执行期间的中文任务名）。
// 图标选型依据（都拿实机截图/素材库核对过，别再凭文件名猜语义）：
//   冒险 = cap_compass（右侧竖栏最上那个「冒险页」按钮用的就是它）
//   学习 = study_book（书本+铅笔）—— 与指南针同批的 inline_icons 里那张
//          `pet_home_03951_47023.png`；**注意素材库里不少图标是哈希文件名**
//          （pet_home_01470_64134.png 才是 PK 字样、pet_home_01314_68535.png 才是
//          橙色 SOAP），按 `*pk*`/`*soap*` 搜文件名是搜不到的，得按图形找
//          （总览图做法见 qqpet_assets/web/INTEGRATION.md）
//   打工 = work_coin（爪印金币 **带绿色上升箭头**）—— 素材库 `inline_icons/
//          pet_home_03481_15399.png`（= 命名表里的 coin_paw_small.png），游戏底部
//          「打工中」状态条用的就是它；`store_money.png`（= coin-48@2x）是**不带箭头**
//          的另一版，别拿它顶替。**也不是** work_logo 公文包（那是出门地图页
//          「职业小镇」入口的图标），这三个曾被我弄混
//   护理 = soap（橙色 SOAP 香皂，素材库原件 pet_home_01314_68535.png）
//   等待中/已停止 = globe（地球，item_globe = pet_home_01997_36242.png）
const RUNICON={
  '上课':'/qp-icons/official/study_book.png',
  '学习':'/qp-icons/official/study_book.png',
  '打工':'/qp-icons/official/work_coin.png',
  '雇佣打工':'/qp-icons/official/work_coin.png',
  '雇佣好友':'/qp-icons/official/work_coin.png',
  '被雇佣检查':'/qp-icons/official/work_coin.png',
  '冒险':'/qp-icons/official/cap_compass.png',
  '护理':'/qp-icons/official/soap.png',
  '好友护理':'/qp-icons/official/soap.png',
  '踩踩':'/qp-icons/official/cap_paw.png',   // 同胶囊行「今日踩踩」那颗
  'PK':'/qp-icons/official/pk_words.png',
  '福袋':'/qp-icons/official/cap_coin.png',
};
const RUNICON_DEFAULT='/qp-icons/official/globe.png';   // 等待中/已停止：地球（item_globe）
function setRunIcon(key, stopped){
  const el=$('#runnerIcon'); if(!el) return;
  const src=RUNICON[key]||RUNICON_DEFAULT;
  if(el.getAttribute('src')!==src) el.setAttribute('src', src);
  // 已停止：灰度压暗，避免"没在跑却亮着"的误读
  el.style.filter=stopped?'grayscale(1) opacity(.55)':'';
}
// 把当前任务映射到房间场景，切 html[data-scene]（CSS 变量作用域要求写在 html 上）。
// 背景库见下面的 SCENE_INFO：**5 套官方房间场景**（room-*，家/喂食/洗澡/学习/商店）
// + **15 款官方「装扮 → 背景」家居背景**（home-*，从官方 APP 里逐张抓的预览，
// 见 qqpet_assets/tools/extract_home_bgs.py 的由来与复现步骤）。
// 映射依据：喂食->feed、洗澡/护理->shower、学习/记录类->record，其余回 main。
// 注意 etaKind 是"上课/打工/冒险"这类进行中活动名，优先级高于队列里的当前任务名
// （队列的 current 可能还是上一项，进行中活动才是"此刻在干什么"）。
const SCENE_OF={care:'feed',friend_care:'feed',school:'record',work:'record',
                hire_friend:'record',adventure:'main',visit:'main',pk:'main',gift_bag:'main'};
// —— 背景库：键 = html[data-scene] 的取值 ——
// file/dark：static/qp-icons/bg/<file>.jpg（dark 缺省 = 深色模式下沿用同一张，
//   家居背景在官方 APP 里没有夜间版）；
// sb/sbDark：该图**顶部实测色**，PWA 独立窗口的状态栏那条带取它（见 syncThemeColor）。
//   色值只在这里维护一份；CSS `:root` 里的 --qp-room-*/--qp-sb-* 只当"JS 还没跑"的首帧兜底。
const SCENE_INFO={
  main:{name:'主房间',file:'room-main',dark:'room-main-dark',sb:'#D5A758',sbDark:'#A9722D'},
  feed:{name:'喂食区',file:'room-feed',dark:'room-feed-dark',sb:'#CA9F5B',sbDark:'#C39145'},
  shower:{name:'浴室',file:'room-shower',dark:'room-shower-dark',sb:'#E7BC6C',sbDark:'#A3651D'},
  record:{name:'教室 / 打工',file:'room-record',dark:'room-record-dark',sb:'#CAA05C',sbDark:'#C18C3A'},
  store:{name:'商店',file:'room-store',dark:'room-store-dark',sb:'#BAD2FE',sbDark:'#2A2E38'},
  // 官方「装扮 → 背景」15 款（顺序照官方页面从上到下、左到右）
  'home-yueer':{name:'月儿圆圆',file:'home-yueer',dark:'home-yueer-dark',sb:'#E0BA96',sbDark:'#252C46'},
  'home-sunset':{name:'朝朝落霞',file:'home-sunset',dark:'home-sunset-dark',sb:'#E9EEFD',sbDark:'#B7ADB8'},
  'home-starry':{name:'夕夕星河',file:'home-starry',dark:'home-starry-dark',sb:'#E6ECFE',sbDark:'#BFB0B7'},
  'home-ocean':{name:'浪花泡泡鱼',file:'home-ocean',dark:'home-ocean-dark',sb:'#B2E1FB',sbDark:'#5F8FB8'},
  'home-nordic':{name:'简约星阁',file:'home-nordic',dark:'home-nordic-dark',sb:'#A9AAB5',sbDark:'#C5BAB3'},
  'home-coast':{name:'意式海岸',file:'home-coast',dark:'home-coast-dark',sb:'#EFE4E2',sbDark:'#D7BBA1'},
  'home-geo':{name:'撞色几何',file:'home-geo',dark:'home-geo-dark',sb:'#F7D374',sbDark:'#D4A976'},
  'home-mint':{name:'薄荷清新',file:'home-mint',dark:'home-mint-dark',sb:'#BDBEBA',sbDark:'#D1CCC1'},
  'home-sunny':{name:'暖阳午后',file:'home-sunny',dark:'home-sunny-dark',sb:'#ECD0C0',sbDark:'#DCC7B9'},
  'home-greyblue':{name:'沉稳灰蓝',file:'home-greyblue',dark:'home-greyblue-dark',sb:'#99A6B6',sbDark:'#C0BCBE'},
  'home-pink':{name:'粉色童话',file:'home-pink',dark:'home-pink-dark',sb:'#FBDEDA',sbDark:'#F2C5BF'},
  'home-green':{name:'绿色童话',file:'home-green',dark:'home-green-dark',sb:'#B9D1BA',sbDark:'#CAC9AF'},
  'home-blue':{name:'蓝色童话',file:'home-blue',dark:'home-blue-dark',sb:'#CCDDED',sbDark:'#D1D8E0'},
  'home-snow':{name:'蓝色雪花',file:'home-snow',dark:'home-snow-dark',sb:'#7A9CBE',sbDark:'#6383A2'},
  'home-yellowpaw':{name:'黄色爪爪',file:'home-yellowpaw',dark:'home-yellowpaw-dark',sb:'#DAA95D',sbDark:'#CEA367'},
  // 官方「宠物职业小镇」7 个打工地点主题（到对应职业解锁）+ 高级学院毕业奖励
  // （素材包来源同上一批：vas_material_folder/petHomeBackground.<id>.zip 的 normal_bg{,_dark}）
  'career-caihong':{name:'彩虹画室',file:'career-caihong',dark:'career-caihong-dark',sb:'#DFCFCB',sbDark:'#BC9076'},
  'career-miwu':{name:'迷雾侦探所',file:'career-miwu',dark:'career-miwu-dark',sb:'#8E8B7E',sbDark:'#3D3630'},
  'career-zhuying':{name:'竹影武馆',file:'career-zhuying',dark:'career-zhuying-dark',sb:'#8499A0',sbDark:'#2F4064'},
  'career-shanyao':{name:'闪耀星屋',file:'career-shanyao',dark:'career-shanyao-dark',sb:'#D7D2F0',sbDark:'#2C2664'},
  'career-yunduo':{name:'云朵梦舍',file:'career-yunduo',dark:'career-yunduo-dark',sb:'#106AC6',sbDark:'#101A62'},
  'career-xingchen':{name:'星尘魔法塔',file:'career-xingchen',dark:'career-xingchen-dark',sb:'#4E3163',sbDark:'#2C2352'},
  'career-gulu':{name:'咕噜厨房',file:'career-gulu',dark:'career-gulu-dark',sb:'#E8CAAF',sbDark:'#A17559'},
  'career-graduate':{name:'高级学院毕业',file:'career-graduate',dark:'career-graduate-dark',sb:'#ECD4CB',sbDark:'#CEC9BA'},
};
const SCENE_ORDER=Object.keys(SCENE_INFO);   // 长按循环顺序 + 选择面板顺序（= 上面声明顺序）
let lastScene='';
// 最近一帧的任务场景输入：手动切回「自动」时要立刻按它重算，不必等下一次数据刷新。
let autoCurKey='', autoEtaKind='';
function computeScene(curKey, etaKind){
  let scene='main';
  if(etaKind.indexOf('洗澡')>=0||etaKind.indexOf('护理')>=0) scene='shower';
  else if(etaKind.indexOf('上课')>=0||etaKind.indexOf('学习')>=0) scene='record';
  else if(etaKind.indexOf('打工')>=0) scene='record';
  else if(curKey) scene=SCENE_OF[curKey]||'main';
  return scene;
}
function isDarkTheme(){
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}
// 背景图版本号：**换图后必须 +1**。/qp-icons/* 走 Cache-Control: max-age=3600，
// 同名覆盖时浏览器一小时以内仍显示旧图（本机踩过：新素材已上线但页面还是旧的）。
// 房间场景 + 家居背景一共 25 张，统一用这一个版本号（CSS 里的静态声明也带同一个）。
const BG_VER='5';
function sceneBgFile(key){
  const info=SCENE_INFO[key]; if(!info) return '';
  return (isDarkTheme() && info.dark) ? info.dark : info.file;
}
function sceneBgUrl(key){
  const file=sceneBgFile(key);
  return file ? "url('/qp-icons/bg/"+file+".jpg?v="+BG_VER+"')" : '';
}
function setSceneAttr(scene){
  if(scene===lastScene) return;
  lastScene=scene;
  const root=document.documentElement, info=SCENE_INFO[scene];
  root.setAttribute('data-scene',scene);  // 必须写 html（见 CSS 注释）
  // 背景图与状态栏色都**内联写到 html**：CSS 里那套 --qp-room-*/--qp-sb-* 只够描述 5 套
  // 房间场景；家居背景 15 套 × 明/暗 = 30 张，逐个写 CSS 规则会重复两份表。
  // 明暗两张图来自官方素材包（petHomeBackground.<id>.zip 里的 normal_bg / normal_bg_dark），
  // 暗版是夜灯/月光版、不是同一张调暗，所以深色模式必须换图（sceneBgFile 用 info.dark）。
  if(info){
    root.style.setProperty('--qp-room', sceneBgUrl(scene));
    const sb=(isDarkTheme() && info.sbDark) ? info.sbDark : info.sb;
    if(sb) root.style.setProperty('--qp-statusbar', sb);
  }
  if(curTab==='main') syncThemeColor('main');
}
function applyScene(curKey, etaKind){
  autoCurKey=curKey; autoEtaKind=etaKind||'';
  if(sceneManual!=='auto') return;   // 手动固定背景时不跟随任务（见 selectScene）
  setSceneAttr(computeScene(curKey, autoEtaKind));
}

// ---- 房间背景手动切换（总览页左上角第 1 个圆钮 #btnScene）----
// 官方那个位置是"返回"，但总览页就是根页面（点了永远早退，曾是置灰死键），
// 现改为换背景：**点按 = 打开选择面板**（21 张：自动 + 5 房间 + 15 家居），
// **长按 = 直接切下一张**（快速预览用）。
// **手动选择必须暂停"按当前任务自动换背景"**，否则每 6 秒一次的 refreshData 会立刻
// 把背景改回任务场景 —— 用户看到的就是"点了没用、自己弹回去"。
// 选择存 localStorage('qpet_scene')，刷新/重开 PWA 后保持；选回「自动」才恢复跟随任务。
let sceneManual='auto';
try{
  const sv=localStorage.getItem('qpet_scene');
  if(sv==='auto'||SCENE_INFO[sv]) sceneManual=sv;
}catch(e){}
let sceneToastTimer=null;
function sceneToast(msg){
  const el=$('#sceneToast'); if(!el) return;
  el.textContent='房间背景：'+msg;
  el.classList.add('on');
  if(sceneToastTimer) clearTimeout(sceneToastTimer);
  sceneToastTimer=setTimeout(()=>el.classList.remove('on'),1600);
}
function sceneName(key){
  return key==='auto' ? '自动（跟随任务）' : ((SCENE_INFO[key]||{}).name||key);
}
// 按当前 sceneManual 重设背景（初始首帧、深色模式切换、选完都走它）
function applyManualScene(){
  lastScene='';            // 强制重设（深色切换时场景键没变、但图要换）
  if(sceneManual==='auto') applyScene(autoCurKey, autoEtaKind);
  else setSceneAttr(sceneManual);
  const btn=$('#btnScene');
  if(btn) btn.title='房间背景（点按选择 / 长按切下一张）· 当前：'+sceneName(sceneManual);
}
function markSceneSheet(){
  const grid=$('#bgSheetGrid'); if(!grid) return;
  grid.querySelectorAll('.bgtile').forEach(t=>t.classList.toggle('on', t.dataset.bg===sceneManual));
}
function selectScene(key, quiet){
  sceneManual=key;
  try{ localStorage.setItem('qpet_scene',key); }catch(e){}
  applyManualScene();
  markSceneSheet();
  if(!quiet) sceneToast(sceneName(key));
}
function cycleScene(){
  const i=SCENE_ORDER.indexOf(sceneManual);   // sceneManual==='auto' 时 i=-1 → 从第一张开始
  selectScene(i+1<SCENE_ORDER.length ? SCENE_ORDER[i+1] : 'auto');
}
// —— 选择面板：21 张缩略图，点一张即生效（同时存 localStorage）——
function buildSceneSheet(){
  const grid=$('#bgSheetGrid'); if(!grid||grid.dataset.built) return;
  let html='<button type="button" class="bgtile" data-bg="auto">'
         + '<span class="bgthumb autothumb">自动</span>'
         + '<span class="bgname">跟随任务</span></button>';
  SCENE_ORDER.forEach(k=>{
    const info=SCENE_INFO[k];
    // 缩略图跟着深浅色取对应文件（房间场景有 -dark 版），跟实际应用的那张保持一致
    html+='<button type="button" class="bgtile" data-bg="'+k+'">'
        + '<img class="bgthumb" loading="lazy" src="/qp-icons/bg/'+sceneBgFile(k)+'.jpg?v='+BG_VER+'" alt="">'
        + '<span class="bgname">'+info.name+'</span></button>';
  });
  grid.innerHTML=html;
  grid.dataset.built='1';
  markSceneSheet();
}
function openSceneSheet(){
  buildSceneSheet();
  const el=$('#bgSheet'); if(!el) return;
  el.classList.add('on');
  el.setAttribute('aria-hidden','false');
}
function closeSceneSheet(){
  const el=$('#bgSheet'); if(!el) return;
  el.classList.remove('on');
  el.setAttribute('aria-hidden','true');
}
// 圆钮绑定：点按开面板、长按（500ms）切下一张。
// 长按后浏览器仍会补一次 click，用 lpFired 吃掉它（否则会"切一张又弹出面板"）。
(function setupSceneBtn(){
  const btn=$('#btnScene'); if(!btn) return;
  let lpTimer=null, lpFired=false;
  const cancelLp=()=>{ if(lpTimer){ clearTimeout(lpTimer); lpTimer=null; } };
  btn.addEventListener('pointerdown', ()=>{
    lpFired=false; cancelLp();
    lpTimer=setTimeout(()=>{ lpTimer=null; lpFired=true; cycleScene(); }, 500);
  });
  ['pointerup','pointerleave','pointercancel'].forEach(ev=>btn.addEventListener(ev,cancelLp));
  btn.addEventListener('contextmenu', e=>e.preventDefault());   // 别弹系统菜单
  btn.addEventListener('click', e=>{
    e.preventDefault(); e.stopPropagation();
    if(lpFired){ lpFired=false; return; }
    openSceneSheet();
  });
  const sheet=$('#bgSheet');
  if(sheet) sheet.addEventListener('click', e=>{
    const tile=e.target.closest && e.target.closest('.bgtile');
    if(tile){ selectScene(tile.dataset.bg); closeSceneSheet(); return; }
    if(e.target.closest && e.target.closest('[data-bgclose]')) closeSceneSheet();
  });
})();

// ---- 宠物状态环 + 状态面板（官方资料卡那枚三层同心环） ----
// 三圈固定对应：外=体力(蓝 #--energy)、中=清洁(绿 --clean)、内=心情(橙 --mood)，
// 每圈按 0~100 画弧（未满部分是灰底 trk）。数据 = /api/data 的 status
// （runs/status_cache.json，护理巡检时 OCR 状态面板写入）；没读到 → dashoffset=整圈
// （只看见灰底），面板里数值显示 '--'。
// 面板是只读展示：官方点环展开的体力/清洁/心情 + 每行右侧 › 进详情，这里不做假箭头。
const PET_STATS=[['energy','体力','e'],['clean','清洁','c'],['mood','心情','m']];
function petVal(st,k){
  const raw=(st||{})[k];
  if(raw===null||raw===undefined||raw==='') return null;
  const n=Number(raw);
  return isFinite(n)?n:null;
}
function setRingArc(id, v){
  const el=document.getElementById(id); if(!el) return;
  const r=parseFloat(el.getAttribute('r'))||0, C=2*Math.PI*r;
  const pct=(v===null)?0:Math.max(0,Math.min(100,v));
  el.setAttribute('stroke-dasharray', C.toFixed(2));
  el.setAttribute('stroke-dashoffset', (C*(1-pct/100)).toFixed(2));
}
// 三行：图标(官方 status_*.png，从真机截图上抠的) + 名称 + 数值 + 进度条 + 右侧 ›。
// › 在官方是"进体力值/清洁值/心情值详情页"，本工具没有那些页面，所以只作视觉还原
// （CSS 里 pointer-events:none，避免做成点了没反应的假按钮）。
function renderPetRows(st){
  const box=$('#petRows'); if(!box) return;
  box.innerHTML=PET_STATS.map(([k,name,cls])=>{
    const v=petVal(st,k), pct=(v===null)?0:Math.max(0,Math.min(100,v));
    return '<div class="prow">'
      +'<img class="pico" src="/qp-icons/official/status_'+k+'.png" alt="">'
      +'<span class="pname">'+name+'</span>'
      +'<span class="pval">'+(v===null?'--':v)+'</span>'
      +'<span class="pbar"><i class="'+cls+'" style="width:'+pct+'%"></i></span>'
      +'<span class="pchev">›</span>'
      +'</div>';
  }).join('');
}
function renderPetStatus(st){
  st=st||{}; window.__petStatus=st;
  const v=PET_STATS.map(([k])=>petVal(st,k));
  setRingArc('arcEnergy', v[0]); setRingArc('arcClean', v[1]); setRingArc('arcMood', v[2]);
  const btn=$('#idRing');
  if(btn) btn.title='体力 '+(v[0]===null?'--':v[0])+' · 清洁 '+(v[1]===null?'--':v[1])
    +' · 心情 '+(v[2]===null?'--':v[2])+'（点击展开/收起）';
  renderPetRows(st);   // 展开着的时候跟着 6 秒刷新一起更新
}
// 展开/收起：官方是"点状态环 → 胶囊原位向下展开"，底部 ^ 收起（真机实测 2026-09-28）。
function togglePetCard(open){
  const card=$('#idCard'); if(!card) return;
  const want=(open===undefined)? !card.classList.contains('open') : !!open;
  card.classList.toggle('open', want);
  if(want) renderPetRows(window.__petStatus||{});
}
(function setupPetRing(){
  const btn=$('#idRing'); if(!btn) return;
  btn.addEventListener('click', e=>{ e.preventDefault(); e.stopPropagation(); togglePetCard(); });
  const fold=$('#idFold');
  if(fold) fold.addEventListener('click', e=>{ e.preventDefault(); e.stopPropagation(); togglePetCard(false); });
})();

let etaRemain=null, etaClock='', schedOn=false;
let logAuto=true, logFilter='';
try{ logAuto = localStorage.getItem('qpet_logAuto')!=='0'; }catch(e){}

function pad(n){return String(n).padStart(2,'0')}
function hms(sec){sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;return (h?h+':':'')+pad(m)+':'+pad(s)}

async function j(u){const r=await fetch(u,{cache:'no-store'});if(!r.ok)throw new Error(r.status);return await r.json()}

function renderData(d){
  const todayStr=(d.now||'').slice(0,10);
  // 头部
  const dot=$('#schedDot');
  dot.className='avatar '+(d.scheduler.alive?'on':'off');
  // 状态环现在画的是**宠物体力/清洁/心情**（官方三层同心环），不再兼任"调度器在跑"的
  // 指示灯——那个信息由左侧头像的绿/红描边 + 下面这行文案承担（历史实现把环涂红，
  // 与官方的含义冲突，用户指出后改掉）。
  renderPetStatus(d.status||{});
  $('#schedTxt').textContent=d.scheduler.alive?('运行中 · 已跑 '+(d.scheduler.uptime||'')):'未运行';
  // 调度器卡片
  if($('#runnerState')){
    const sch=d.scheduler||{};
    $('#runnerDot').className='dot '+(sch.alive?'on':'off');
    $('#runnerMeta').textContent=sch.alive?('PID '+sch.pid+(sch.uptime?(' · 已跑 '+sch.uptime):'')):'未运行';
    $('#btnRunnerStart').disabled=!!sch.alive;
    $('#btnRunnerStop').disabled=!sch.alive;
  }
  // 金币
  const st=d.status||{};
  $('#coins').textContent=st.coins!=null?st.coins:'--';
  $('#coinsAt').textContent=(st.coins!=null&&st.updated)?'· '+st.updated.slice(11,16):'';
  // 打工卡片
  etaRemain=(d.work_eta&&d.work_eta.remaining!=null)?d.work_eta.remaining:null;
  etaClock=d.work_eta?d.work_eta.eta_clock:'';
  schedOn=(d.scheduler||{}).alive;
  const td=d.today_duration;
  let wdHtml='';
  if(td){
    const eff=(td.eff_pct!=null&&td.eff_pct<100)?('<span style="color:#d97706">效率 '+td.eff_pct+'%</span>'):'效率 100%';
    const nxt=(td.next_pct!=null&&td.next_in_min!=null)?('（再 '+td.next_in_min+' 分降到 '+td.next_pct+'%）'):'';
    wdHtml='今日：学习 '+td.learn_min+' 分 · 打工 '+td.work_min+' 分 · 合计 '+(td.total_min??(td.learn_min+td.work_min))+' 分 · '+eff+nxt;
  }
  const rs=$('#runnerState'), rh=$('#runnerHint'), rsub=$('#runnerSub');
  // 队列状态是不是"当前这个调度器进程"写的：重启后上一轮写在文件里的
  // pending（如"上课"）会一直留着，照读会让界面继续显示"上课中"（用户实报：
  // 宠物被召回、停掉调度器再启动，显示还是在上课）。服务端 mark_starting()
  // 会写 starting:true + 本次 PID，这里对不上就当"启动检查中"。
  // 宽限期（10 分钟）：legacy 引擎不写队列状态，不能让"启动中"永远挂着。
  const q=d.queue||{}, schInfo=d.scheduler||{};
  const qStale=!!(q.starting||q.stopped||(q.pid&&schInfo.pid&&q.pid!==schInfo.pid));
  const upSecs=schInfo.uptime?schInfo.uptime.split(':').reduce((a,b)=>a*60+Number(b),0):0;
  const qStarting=qStale&&upSecs<600;
  const curTask=(!qStale&&q.current)?String(q.current):'';
  const etaKind=(d.work_eta&&d.work_eta.kind)?String(d.work_eta.kind):'';
  // 状态 key：图标与文案共用同一个，避免出现「冒险中」配着福袋图标这种错位。
  //   ① remaining>0 的 work_eta = 主任务活动真在进行（带倒计时，最准）
  //   ② 否则队列 current = 调度器正在跑的场景（护理/PK/踩踩/福袋…）
  //   ③ 再否则 work_eta 残留 = 已到点、正在收尾
  // ① 必须判 >0：work_eta 在活动结束后 10 分钟内仍会返回（remaining 被 clamp 成 0），
  // 只看“非 null”会让图标和文案一直停在上一项活动上（实测：冒险已结束、队列在跑福袋）。
  const busyEta=(etaRemain>0&&etaKind)?etaKind:'';
  const finishing=(!busyEta&&!curTask&&etaRemain!=null&&etaKind)?etaKind:'';
  const stateKey=busyEta||curTask||finishing;
  if(!schedOn){
    // 调度器未运行：不引用日志里的旧“预计结算”行（会残留“进行中 剩余00:00”误导）
    rs.textContent='已停止';
    rh.textContent='';
    rsub.textContent='已停止：手机不会被自动操作；随时可再启动';
    $('#workSub').innerHTML=wdHtml;
    setRunIcon(null, true);
  }else if(qStarting){
    // 刚起来/上一轮留下的状态：调度器一启动会先出门实测宠物当前状态，
    // 实测完写出带自己 PID 的状态后才显示"上课中/打工中"等真实结论
    rs.textContent='启动中';
    rh.textContent=d.last_line?d.last_line.replace(/^\[[\d:]+\]\s*/,'').slice(0,60):'启动检查中…';
    $('#workSub').innerHTML=wdHtml;
    rsub.textContent='';
    setRunIcon(null);
  }else if(busyEta){
    rs.textContent=busyEta+'中';
    rh.textContent='预计 '+etaClock+' 结束';
    $('#workSub').textContent='剩余 '+hms(etaRemain)+' · 结束后自动开启下一项';
    rsub.textContent='';
    setRunIcon(busyEta);
  }else if(finishing){
    rs.textContent=finishing+'中';
    rh.textContent='预计 '+etaClock+' 结束';
    $('#workSub').textContent='收尾中… · 结束后自动开启下一项';
    rsub.textContent='';
    setRunIcon(finishing);
  }else{
    // current 非空 = 调度器正在跑某个场景（护理/PK/踩踩/福袋…），此时不该说“等待中”
    rs.textContent=stateKey?(stateKey+'中'):'等待中';
    rh.textContent=d.last_line?d.last_line.replace(/^\[[\d:]+\]\s*/,'').slice(0,60):'';
    $('#workSub').innerHTML=wdHtml;
    rsub.textContent='';
    setRunIcon(stateKey);
  }
  if(rsub) rsub.style.display=rsub.textContent?'':'none';
  // 统计瓦片
  const pg=d.progress||{}, cfg=d.config||{};
  const vv=pg.visit&&pg.visit.learned!=null?pg.visit.learned:null;
  const vvMax=cfg.visit_per_day||10;
  $('#visitTxt').textContent=(vv!=null?vv:'--')+'/'+vvMax;
  $('#visitBar').style.width=(vv!=null?Math.min(100,vv/vvMax*100):0)+'%';
  const pp=pg.pk&&pg.pk.learned!=null?pg.pk.learned:null;
  const ppMax=cfg.pk_per_day||15;
  $('#pkTxt').textContent=(pp!=null?pp:'--')+'/'+ppMax;
  $('#pkBar').style.width=(pp!=null?Math.min(100,pp/ppMax*100):0)+'%';
  const av=pg.adventure&&pg.adventure.learned!=null?pg.adventure.learned:0;
  $('#advTxt').textContent=av+'/'+(cfg.adventure_times||1);
  // 今日学习+打工（一个胶囊只显示【合计总时长】小时数，不再区分学习/打工）。
  // 数据 = school_progress.study_secs + work_progress.work_secs（秒，各自累计；
  // 学习/打工的明细只在 title 悬浮提示与统计页里看，胶囊上不分开显示）。
  // 主数值 = "已用/目标"（如 7.5/12h），进度条 = 单段橙条按 已用/目标 填充
  //（原来是学习橙+打工蓝两段叠加，用户要求不再区分 → 已合并成一条）。
  // 目标（分母）取当天的【合计停止点】：设置页只暴露一个值（stop_total_hours，
  // 后端由 efficiency_tier2_hours / daily_hour_limit / work_stop_hours 折算，
  // 保存时三键同值），这里直接用它；老配置三项不一致时退回三者里 >0 的最大值。
  // 不能拿 study_quota+work_quota 当分母：两者都放开成 24 时等于 48h，条永远是空的
  //（实测配额 24+24、打工 10h 时蓝段只有 21%，看不出进度）；停止点都没配时才退回配额合计。
  // 注意 kind 仍要参与判断——work_eta 是"上课/打工/冒险"共用模板，
  // 只判有无会把"正在上课"错算成"正在打工"（曾显示 0+1 实际在上课）。
  // etaKind 已在上面状态卡那段声明（图标与文案共用同一个 key）
  const busy=(schedOn&&etaRemain!=null);
  const hrs=s=>((s||0)/3600).toFixed(1).replace(/\.0$/,'');
  const sc=pg.school&&pg.school.learned!=null?pg.school.learned:0;
  const scHrs=Number(hrs(pg.school&&pg.school.study_secs));
  const wk=pg.work&&pg.work.learned!=null?pg.work.learned:0;
  const wkHrs=Number(hrs(pg.work&&pg.work.work_secs));
  const scBusy=busy&&etaKind.indexOf('上课')>=0;
  const wkBusy=busy&&etaKind.indexOf('打工')>=0;
  // 合计小时：先把两段秒数相加再换算，避免各自 toFixed(1) 后相加的舍入误差
  const totalHrs=Number(hrs(((pg.school&&pg.school.study_secs)||0)
                            +((pg.work&&pg.work.work_secs)||0)));
  // 合计停止点（小时）：>0 才有效（0 = 不限，不参与取上限）
  const edt=d.editable||{};
  const stopPts=[edt.stop_total_hours,edt.efficiency_tier2_hours,edt.daily_hour_limit,
                 edt.work_stop_hours].map(Number).filter(v=>v>0);
  const targetHrs=stopPts.length?Math.max.apply(null,stopPts)
    :((Number(cfg.study_quota_hours)||0)+(Number(cfg.work_quota_hours)||0));
  $('#swTxt').textContent=totalHrs+'h'+(targetHrs?('/'+targetHrs+'h'):'');
  $('#swBarTotal').style.width=(targetHrs
    ?Math.min(100,Math.max(0,totalHrs/targetHrs*100)):0)+'%';
  // 胶囊太窄放不下，长文案转到 title 上（悬浮/长按可见）
  const swTip='今日学习+打工合计 '+totalHrs+' 小时'
    +(targetHrs?('（目标 '+targetHrs+'h = 合计停止点）'):'')
    +' · 学习 '+sc+' 节 '+scHrs+'h / 打工 '+wk+' 次 '+wkHrs+'h'
    +((scBusy||wkBusy)?(' · 正在进行：'+(d.work_eta.kind||'')):'');
  const _capSw=$('#capSw'); if(_capSw) _capSw.title=swTip;
  $('#swTxt').title=swTip;
  const ed=(pg.exp_daily&&pg.exp_daily.done)?'✓ 完成':'未完成';
  $('#expTxt').textContent=ed;
  // 队列：MAA 风格任务开关列表（勾选=启用该任务，写入 config 下轮生效）
  // q 是上面判过新鲜度的队列状态：重启后文件里还是上一轮的内容（含"上课 待结算"），
  // 这类状态按"没在跑"渲染（用配置里的启用状态），别把上一轮的任务当现状
  const qt=q.tasks||{};
  const qLive=(d.scheduler||{}).alive && !qStale;
  const qOrder=(cfg.task_order||[]);
  const qRank=k=>{const i=qOrder.indexOf(k);return i<0?999:i;};
  let rows='';
  const cur=qStale?'':(q.current||'');
  const curMap={'上课':'school','学习':'school','打工':'work','冒险':'adventure',
                '护理':'care','踩踩':'visit','PK':'pk','好友护理':'friend_care',
                '福袋':'gift_bag','雇佣好友':'hire_friend'};
  const curKey=cur?(curMap[cur]||Object.keys(TASKNAME).find(k=>TASKNAME[k]===cur)||''):null;
  // 主任务组"延时收尾"期间（宠物在游戏里上课/打工/冒险/被雇佣打工），调度器已经回主页去跑
  // 别的支线了，所以 **current 是空的**，只有 pending 描述（'雇佣打工' 等）—— 早先只认 current，
  // 于是"宠物正在被雇佣打工、列表里雇佣好友那一行却不亮"（用户实报）。
  // 优先用调度器新写的 pending_key（精确）；老调度器没这个字段时按描述兜底映射。
  const pendMap={'上课':'school','打工':'work','雇佣打工':'hire_friend','冒险':'adventure'};
  // 描述优先于 pending_key：描述是场景写 pending 时留下的原文，最可靠；
  // pending_key 是调度器额外写的键（老进程没有、极少数路径可能认错键），只作兜底。
  const pendKey=pendMap[q.pending]||q.pending_key||'';
  applyScene(curKey, (d.work_eta&&d.work_eta.kind)?String(d.work_eta.kind):'');
  // 任务行的"目标"副标题：好友护理/雇佣好友这类要指名道姓的任务，在任务名后面
  // 跟一个浅色小字（用户："好友护理后面能显示护理的谁吗"）。数据来自设置页快照，
  // 改配置后下一轮 /api/data 就跟着变；名字过长由 CSS 省略号截断。
  const _ed=d.editable||{};
  const _first=c=>String(c||'').split(/[,，、]/)[0].trim();
  const TASKSUB={
    friend_care:_first(_ed.friend_care_name),
    hire_friend:_first(_ed.hire_name||_ed.hire_friend_name),
  };
  const rowOf=(k,on,st,nx)=>{
    const isRun=curKey&&k===curKey;
    const isPend=pendKey&&k===pendKey;      // 宠物正在做、等收尾结算
    // 行右侧状态：只给"有信息量"的几种写字 —— 正常可执行留空（每行都挂"可执行"太吵），
    // 有等待点就给时间（等待 · 14:03），跑着的、跑完的、今天收工的、禁用的各一句。
    const sub=on?(TASKSUB[k]||''):'';   // 禁用行不显示对象（那行本来就不跑）
    let det='';
    if(!on) det='<span class="off-t">已禁用</span>';
    else if(isRun) det='<span class="run">执行中</span>';
    else if(isPend) det='<span class="run">进行中</span>';
    else if(st==='waiting') det=(qt[k]&&qt[k].next)?('等待 '+qt[k].next.slice(11,16)):'等待';
    else if(st==='done') det='<span class="done">✓ 今日完成</span>';
    else if(st==='dead') det='今日结束';
    // 带"对象"的行（好友护理/雇佣好友）可用宽度只剩 ~100u：状态压成极简形式
    //（时间 / ✓ / —），把位置让给对象名；"跑着"这件事由任务名后的 ▶ 表示（见 runMark）。
    if(sub&&det){
      if(isRun||isPend) det='';
      else if(st==='waiting') det=(qt[k]&&qt[k].next)?qt[k].next.slice(11,16):'';
      else if(st==='done') det='<span class="done">✓</span>';
      else if(st==='dead') det='—';
    }
    const done=(st==='done'||st==='dead');
    const tag=TTAG[k]?('<span class="ttag">'+TTAG[k]+'</span>'):'';
    // 按需求：右侧的"已启用/已禁用"状态文字已移除，
    // 勾选框从左侧移到原状态文字的位置（最右）。
    // 任务名前的彩色图标（img.qico + TASKICON 映射）也已按用户要求删除，
    // 任务行只剩「名称 + 类型标签 + 勾选框」，别再往行首加图标。
    // 两种"活跃"（isRun 执行中 / isPend 进行中）用同一个高亮类：
    // 靠"高亮行上下留 1.5u 间隙"避免相邻两条糊成一片（用户明确不要深浅分级那版）
    return '<div class="mrow'+(done?' done':'')+((isRun||isPend)?' run':'')+(on?'':' off')+'" data-k="'+k+'">'
      +'<span class="mname'+(on?'':' off')+'">'+(TASKNAME[k]||k)+'</span>'
      // 选中/进行中的 ▶ 紧跟在任务名后面（用户："那个被选中的箭头放到任务名的后面去"）
      +((isRun||isPend)?('<span class="marrow" title="'+(isPend?'进行中（等收尾结算）':'正在执行')+'">▶</span>'):'')
      +tag
      // 对象名与状态文字包成"贴右一簇"：auto 边距只加在簇上，这样有对象/没对象的行
      // 右缘都对齐（早先对象名跟在任务名后面 → 看起来像没右对齐，用户实报）
      +'<span class="mright">'
      +(sub?('<span class="msub" title="'+esc(sub)+'">'+esc(sub)+'</span>'):'')
      +(det?('<span class="mdet">'+det+'</span>'):'')
      +'</span>'
      +'<span class="mcb'+(on&&st!=='disabled'?' on':'')+'" data-k="'+k+'"></span></div>';
  };
  // 任务列表分两组（用户要求）：
  //   日常轮巡 = TTAG 里的「循环」类（护理 / 好友护理 / 福袋）—— 按间隔巡检，
  //              顺序固定，不参与拖动
  //   任务顺序 = 其余（每日 / 主线）—— 可拖动排序，松手写回 tasks.order
  const isLoopTask=k=>TTAG[k]==='循环';
  const groupedRows=(keys, renderOne)=>{
    const loop=keys.filter(isLoopTask), rest=keys.filter(k=>!isLoopTask(k));
    let h='';
    if(loop.length){
      h+='<div class="tghd"><span>日常轮巡</span><i></i>'
        +'<span class="tghint">按间隔巡检 · 顺序固定</span></div>';
      for(const k of loop) h+=renderOne(k);
    }
    if(rest.length){
      h+='<div class="tghd"><span>任务顺序</span><i></i>'
        +'<span class="tghint">按住拖动排序</span></div>';
      for(const k of rest) h+=renderOne(k);
    }
    return h || keys.map(renderOne).join('');
  };
  if(qLive){
    // 收尾队列：写在标题行右侧（原来单独占一行，视觉上像多了一个任务）
    const _qp=document.getElementById('qPend');
    if(_qp) _qp.innerHTML = q.pending
      ? ('<span class="run">'+q.pending+' 待结算</span>') : '';
    const ks=Object.keys(qt).slice().sort((a,b)=>qRank(a)-qRank(b));
    rows+=groupedRows(ks, k=>{
      const st=qt[k].state||'';
      const nx=qt[k].next?('→ '+(qt[k].next.slice(0,10)===todayStr?'':'明 ')+qt[k].next.slice(11,16)):'';
      return rowOf(k, st!=='disabled', st, nx);
    });
  }else{
    const _qp2=document.getElementById('qPend'); if(_qp2) _qp2.innerHTML='';
    const te=cfg.tasks_enabled||{};
    const keys=qOrder.length?qOrder:Object.keys(te);
    const allKeys=(keys.length?keys:Object.keys(TASKNAME)).slice().sort((a,b)=>qRank(a)-qRank(b));
    rows+=groupedRows(allKeys, k=>rowOf(k, te[k]!==false, 'cfg', ''));
  }
  $('#taskList').innerHTML=rows||'';
  setupTaskDrag();
  // 「未启用：xxx（不参与调度）」提示行已按需求移除
  // 截图
  const shots=d.shots||[];
  if(shots.length){
    $('#shotCard').classList.remove('hide');
    $('#shots').innerHTML=shots.map(s=>'<a href="/files/'+encodeURIComponent(s.name)+'" target="_blank"><img loading="lazy" src="/files/'+encodeURIComponent(s.name)+'"><span class="cap">'+s.mtime+'</span></a>').join('');
  }
  // 好友名单（供表单下拉）：变化时更新，并强制重渲染表单让下拉项生效
  if(Array.isArray(d.friends)){
    const changed = JSON.stringify(d.friends)!==JSON.stringify(FRIENDS);
    FRIENDS = d.friends;
    if(changed && window.__lastEditable && !setDirty) renderSettings(window.__lastEditable);
  }
  if(d.editable) window.__lastEditable=d.editable;
    // 重建表单会销毁正在操作的控件（下拉被自动关闭、输入焦点丢失）。
  // 三种情况都不重建：①有未保存改动 ②焦点在表单内 ③刚有过交互(2s 内)。
  // 见 markDirtyAndSave / __formBusy。
  if(d.editable && !setDirty && !formBusy()) renderSettings(d.editable);
  if(window.__wrapPages) window.__wrapPages();
  renderCfg((d.config||{}).rows);
  // 页面底部的策略行（footer）已按需求移除，这里不再拼文案
}

async function refreshData(){
  try{ renderData(await j('/api/data')); }
  catch(e){ $('#schedDot').className='avatar off'; $('#schedTxt').textContent='连接失败'; }
}

function svgSet(id,inner){const el=document.getElementById(id);if(el)el.innerHTML=inner;}
function drawAdv(){
  const d=window.__adv;if(!d)return;
  const W=340,H=84,pad=8,tx=d.n||100;
  const sx=i=>pad+(Math.max(1,i)-1)/Math.max(1,(tx-1))*(W-2*pad);
  let inner='<line x1="0" y1="42" x2="'+W+'" y2="42" stroke="#e2e5ec" stroke-width="1" stroke-dasharray="4 4"/>';
  const ys0=(d.cum||[]).map(p=>p[1]);
  let mx=Math.max(10,...ys0.map(v=>Math.abs(v)))*1.15;
  const sy0=v=>42-v/mx*34;
  if((d.cum||[]).length>1){
    inner+='<polyline points="'+d.cum.map(p=>sx(p[0]).toFixed(1)+','+sy0(p[1]).toFixed(1)).join(' ')+'" fill="none" stroke="#ea580c" stroke-width="2" stroke-linejoin="round"/>';
    const lp=d.cum[d.cum.length-1];
    inner+='<circle cx="'+sx(lp[0]).toFixed(1)+'" cy="'+sy0(lp[1]).toFixed(1)+'" r="3" fill="#ea580c"/>';
  }
  if(window.__advSel&&window.__advSel.chart==='cum'){const cm={};(d.cum||[]).forEach(p=>cm[p[0]]=p[1]);const k=window.__advSel.k;if(k in cm){const xx=sx(k).toFixed(1);inner+='<line x1="'+xx+'" y1="4" x2="'+xx+'" y2="80" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3 3"/><circle cx="'+xx+'" cy="'+sy0(cm[k]).toFixed(1)+'" r="4" fill="#ea580c" stroke="#fff" stroke-width="1.5"/>';}}
  svgSet('svgCum',inner);
  let inner2='<line x1="0" y1="42" x2="'+W+'" y2="42" stroke="#e2e5ec" stroke-width="1" stroke-dasharray="4 4"/>';
  const ys1=(d.pts||[]).map(p=>p[1]);
  let mx1=Math.max(10,...ys1.map(v=>Math.abs(v)))*1.2;
  const sy1=v=>42-v/mx1*34;
  if(d.avg!=null){const y=sy1(d.avg);inner2+='<line x1="0" y1="'+y.toFixed(1)+'" x2="'+W+'" y2="'+y.toFixed(1)+'" stroke="#f59e0b" stroke-width="1" stroke-dasharray="5 4"/>';}
  for(const p of (d.pts||[])){inner2+='<circle cx="'+sx(p[0]).toFixed(1)+'" cy="'+sy1(p[1]).toFixed(1)+'" r="2.2" fill="#0ea5e9" opacity=".85"/>';}
  if(window.__advSel&&window.__advSel.chart==='pts'){const dm={};(d.pts||[]).forEach(p=>dm[p[0]]=p[1]);const k=window.__advSel.k;if(k in dm){const xx=sx(k).toFixed(1);inner2+='<line x1="'+xx+'" y1="4" x2="'+xx+'" y2="80" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3 3"/><circle cx="'+xx+'" cy="'+sy1(dm[k]).toFixed(1)+'" r="4" fill="#0ea5e9" stroke="#fff" stroke-width="1.5"/>';}}
  svgSet('svgPts',inner2);
  const st=d.stats||[];
  const sy2=v=>H-pad-(Math.max(0,Math.min(100,v))/100)*(H-2*pad);
  let inner3='<line x1="0" y1="'+sy2(60).toFixed(1)+'" x2="'+W+'" y2="'+sy2(60).toFixed(1)+'" stroke="#ef4444" stroke-width="1" stroke-dasharray="5 4" opacity=".7"/>';
  const defs=[['e',1,'#16a34a'],['c',2,'#0891b2'],['m',3,'#d97706']];
  for(const df of defs){
    const pts=st.filter(r=>r[df[1]]!=null);
    if(pts.length<2)continue;
    inner3+='<polyline points="'+pts.map(r=>sx(r[0]).toFixed(1)+','+sy2(r[df[1]]).toFixed(1)).join(' ')+'" fill="none" stroke="'+df[2]+'" stroke-width="1.8"/>';
    const lp=pts[pts.length-1];
    inner3+='<circle cx="'+sx(lp[0]).toFixed(1)+'" cy="'+sy2(lp[df[1]]).toFixed(1)+'" r="2.6" fill="'+df[2]+'"/>';
  }
  for(const r of st){if(r[4]===1){inner3+='<line x1="'+sx(r[0]).toFixed(1)+'" y1="'+pad+'" x2="'+sx(r[0]).toFixed(1)+'" y2="'+(H-pad)+'" stroke="#ef4444" stroke-width="1" stroke-dasharray="2 3" opacity=".6"/>';}}
  if(window.__advSel&&window.__advSel.chart==='stats'){const k=window.__advSel.k;const rw=st.filter(r=>r[0]===k);if(rw.length){const xx=sx(k).toFixed(1);inner3+='<line x1="'+xx+'" y1="4" x2="'+xx+'" y2="80" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3 3"/>';for(const rr of rw){if(rr[1]!=null)inner3+='<circle cx="'+xx+'" cy="'+sy2(rr[1]).toFixed(1)+'" r="3" fill="#16a34a" stroke="#fff"/>';if(rr[2]!=null)inner3+='<circle cx="'+xx+'" cy="'+sy2(rr[2]).toFixed(1)+'" r="3" fill="#0891b2" stroke="#fff"/>';if(rr[3]!=null)inner3+='<circle cx="'+xx+'" cy="'+sy2(rr[3]).toFixed(1)+'" r="3" fill="#d97706" stroke="#fff"/>';}}}
  svgSet('svgStats',inner3);
}
function renderAdventure(d){
  window.__adv=d;
  if(!d||!d.ok){const m=$('#advMeta');if(m)m.textContent='暂无数据';return}
  const fmtNet=v=>(v>0?'+':'')+v;
  if(d.date==='all') $('#advMeta').textContent='共 '+d.n+' 把 · 今日 '+(d.today_n||0)+' 把（'+fmtNet(d.today_net||0)+'） · 更新 '+(d.updated||'');
  else if(d.date===d.today) $('#advMeta').textContent='共 '+d.n+' 把 · 更新 '+(d.updated||'');
  else $('#advMeta').textContent=(d.date||'').slice(5).replace('-','月')+'日 · 共 '+d.n+' 把 · 更新 '+(d.updated||'');
  renderAdvDates(d);
  const big=$('#advNet');big.textContent=(d.net>0?'+':'')+d.net;
  big.style.color=d.net>0?'var(--gold)':(d.net<0?'#dc2626':'');
  $('#advNetHint').textContent='金币收益合计（结算页口径） · 平均 '+(d.avg>0?'+':'')+d.avg+'/把';
  $('#advSub').textContent='有收益 '+d.win+' 把 · 零收益 '+d.zero+' 把';
  let ch='';
  for(const g of (d.gains||[])){ch+='<span class="chip">'+esc(g[0])+' +'+g[2]+' ×'+g[1]+'</span>';}
  $('#advChips').innerHTML=ch;
  const hasStats=!!((d.stats||[]).length);
  const sv=$('#svgStats'); if(sv) sv.style.display=hasStats?'':'none';
  const cp=$('#capStats'); if(cp) cp.style.display=hasStats?'':'none';
  // 阈值文字用后端给的配置值（体力/清洁可能不同，两个都列）
  const thr=$('#capThr');
  if(thr){
    const e=d.care_energy, c=d.care_clean;
    thr.textContent=(e==null?'--':String(e))+(c!=null&&c!==e?('/'+c):'');
  }
  drawAdv();
  renderAdvList(d);
}
function renderAdvDates(d){
  const row=$('#advDateRow'); if(!row)return;
  const sig=(d.date||'')+'|'+(d.dates||[]).join(',');
  if(row.__sig===sig)return; row.__sig=sig;
  const opts=[];
  for(const dt of (d.dates||[])){
    const lab=dt===d.today?'今天':(dt===d.yesterday?'昨天':dt.slice(5).replace('-','/'));
    opts.push('<option value="'+dt+'"'+(dt===d.date?' selected':'')+'>'+lab+'</option>');
  }
  opts.push('<option value="all"'+(d.date==='all'?' selected':'')+'>全部</option>');
  row.innerHTML='统计范围 <select id="advDate" style="font:inherit;padding:1px 4px">'+opts.join('')+'</select>';
}
const _advDateRow=document.getElementById('advDateRow');
if(_advDateRow) _advDateRow.addEventListener('change',e=>{
  if(e.target&&e.target.id==='advDate'){window.__advDate=e.target.value;refreshAdventure();}
});
let advShowAll=true;
function renderAdvList(d){
  const list=$('#advList'); if(!list)return;
  let arr=(d.recent||[]).slice().reverse().slice(0,400);
  if(!advShowAll) arr=arr.filter(r=>r[2]!==0||r[4]);
  list.innerHTML=arr.map(r=>{
    const v=r[2]; const cls=v>0?'pos':(v<0?'neg':'zero');
    const vt=(v>0?'+':'')+(v==null?'?':v);
    let g=esc(r[3]||'');
    if(r[4]) g+=(g?' ':'')+'<span style="color:#dc2626">扣费'+r[4]+'</span>';
    return '<div class="arow"><span class="ai">#'+r[0]+' '+(r[1]||'').slice(0,11)+'</span><span class="ag">'+g+'</span><span class="av '+cls+'">'+vt+'</span></div>';
  }).join('')||'<div class="arow"><span class="ag">暂无记录</span></div>';
  const b=$('#btnAdvAll'); if(b){b.className='minibtn'+(advShowAll?' on':'');b.textContent=advShowAll?'全部':'仅变化';}
}
const _advBtn=document.getElementById('btnAdvAll');
if(_advBtn) _advBtn.onclick=()=>{advShowAll=!advShowAll; if(window.__adv)renderAdvList(window.__adv);};
window.__advSel=null;
function advMaps(d){
  const m={cum:{},dl:{},gn:{},tm:{},st:{}};
  for(const p of (d.cum||[]))m.cum[p[0]]=p[1];
  for(const p of (d.pts||[]))m.dl[p[0]]=p[1];
  for(const r of (d.recent||[])){m.gn[r[0]]=r[3]||'';m.tm[r[0]]=r[1]||'';}
  for(const r of (d.stats||[])){const k=r[0];if(!(k in m.st)||r[4]===1)m.st[k]=r;}
  return m;
}
function advSelect(chart,k){
  if(!window.__adv)return;
  window.__advSel={chart:chart,k:k};
  drawAdv();
  const m=advMaps(window.__adv);
  const tip=$('#advTip');if(!tip)return;
  if(chart==='stats'){
    const r=m.st[k];
    if(r)tip.innerHTML='#'+k+(m.tm[k]?(' '+m.tm[k].slice(0,5)):'')+' · 体力 <b>'+(r[1]==null?'-':r[1])+'</b> · 清洁 <b>'+(r[2]==null?'-':r[2])+'</b> · 心情 <b>'+(r[3]==null?'-':r[3])+'</b>'+(r[4]===1?'（护理后）':'');
  }else{
    const v=m.dl[k];
    let s='#'+k+(m.tm[k]?(' '+m.tm[k].slice(0,5)):'')+' · 累计 '+((m.cum[k]||0)>0?'+':'')+(m.cum[k]||0)+' · 本趟 '+((v>0?'+':'')+(v==null?'?':v));
    if(m.gn[k])s+=' · '+esc(m.gn[k]);
    tip.innerHTML=s;
  }
}
function advNearest(chart,x){
  const d=window.__adv;if(!d)return;
  const pad=8,tx=d.n||100,W=340;
  const sx0=i=>pad+(Math.max(1,i)-1)/Math.max(1,(tx-1))*(W-2*pad);
  const arr=chart==='stats'?(d.stats||[]).map(r=>r[0]):(d.pts||[]).map(p=>p[0]);
  let best=null,bd=1e9;
  for(const i of arr){const dd=Math.abs(sx0(i)-x);if(dd<bd){bd=dd;best=i;}}
  if(best!=null)advSelect(chart,best);
}
function bindAdvChart(id,chart){
  const el=document.getElementById(id);if(!el||el.__b)return;el.__b=true;
  let down=false;
  const pos=e=>{const rect=el.getBoundingClientRect();return (e.clientX-rect.left)*(340/Math.max(1,rect.width));};
  el.addEventListener('pointerdown',e=>{down=true;advNearest(chart,pos(e));});
  el.addEventListener('pointermove',e=>{if(down)advNearest(chart,pos(e));});
  const up=()=>{down=false;};
  el.addEventListener('pointerup',up);el.addEventListener('pointercancel',up);el.addEventListener('pointerleave',up);
}
bindAdvChart('svgCum','cum');bindAdvChart('svgPts','pts');bindAdvChart('svgStats','stats');
let planDirty=false;
function planBar(t,c,tg,col){
  const w=tg?Math.max(0,Math.min(100,c/tg*100)):0;
  return '<div class="pb"><div class="t"><span>'+t+'</span><span>'+c+' / '+tg+'</span></div><div class="bar"><i style="width:'+w.toFixed(1)+'%;background:'+col+'"></i></div></div>';
}
function planBuildEdit(v){
  $('#planEdit').innerHTML=
    '<label>力量<input type="number" id="pn1" min="0" value="'+(v['力']??0)+'"></label>'+
    '<label>智力<input type="number" id="pn2" min="0" value="'+(v['智']??0)+'"></label>'+
    '<label>魅力<input type="number" id="pn3" min="0" value="'+(v['魅']??0)+'"></label>'+
    '<label>工分<input type="number" id="pn4" min="0" value="'+(v['工分']??0)+'"></label>'+
    '<label>金币<input type="number" id="pn5" min="0" value="'+(v['金币']??0)+'"></label>'+
    '<div class="planeditrow"><span>学园：</span><button class="sw'+(v['初级毕业']?' on':'')+'" id="swPrim"></button><span>初级毕业</span><button class="sw'+(v['中级毕业']?' on':'')+'" id="swMid"></button><span>中级毕业</span></div>';
  for(const id of ['#pn1','#pn2','#pn3','#pn4','#pn5']){ const el=$(id); if(el) el.oninput=()=>{planDirty=true;}; }
  const sp=$('#swPrim'), sm=$('#swMid');
  if(sp) sp.onclick=()=>{sp.classList.toggle('on');planDirty=true;};
  if(sm) sm.onclick=()=>{sm.classList.toggle('on');planDirty=true;};
}
function renderPlan(d){
  if(!d||!d.ok)return;
  window.__plan=d;
  $('#planMeta').textContent='总属性 '+d.total+'/'+d.total_target+' · 更新 '+(d.updated||'');
  $('#planBars').innerHTML=planBar('属性总进度',d.total,d.total_target,'var(--accent)')+planBar('见习解锁',d.jr_n,8,'#16a34a')+planBar('初级解锁',d.ch_n,8,'#ea580c');
  let firstOpen=false;
  $('#planSteps').innerHTML=(d.steps||[]).map(s=>{
    let cls='st',dot='○';
    if(s[2]){cls+=' done';dot='✅';}
    else if(!firstOpen){cls+=' cur';dot='▶';firstOpen=true;}
    return '<div class="'+cls+'"><span class="dot2">'+dot+'</span><span class="tx">'+esc(s[0]+' · '+s[1])+'</span><span class="pr">'+esc(s[3])+'</span></div>';
  }).join('');
  if($('#planLinesMeta')) $('#planLinesMeta').textContent=d.lines_meta||'';
  $('#planLines').innerHTML=(d.lines||[]).map(l=>'<div class="ln"><span>'+esc(l.name)+'</span><span><span class="chipx'+(l.jr?' ok':'')+'">见习</span><span class="chipx'+(l.ch?' ok':'')+'">初级</span></span></div>').join('');
  const w=d.watch||{};
  if($('#watchMeta')) $('#watchMeta').textContent=w.last_check?('上次检查 '+String(w.last_check).slice(11,16)):'';
  if($('#watchBox')){
    let st;
    if(!w.enabled) st='监控已关闭（设置页「职业」区可开）';
    else if(!w.alive) st='调度器未运行 — 启动后自动监控';
    else st='监控中 · '+(w.interval?('每节课后 + 每 '+w.interval+' 分钟兜底'):'每节课后')+(w.stop_study?' · 解锁后自动停学':' · 仅通知');
    let wh='<div class="wstate">'+st+'</div>';
    const evs=(w.events||[]).slice().reverse();
    if(evs.length){
      wh+=evs.map(e=>'<div class="wrow"><span class="wbadge">🎉 '+esc(e.career||'')+'（见习·'+esc(e.name||'?')+'）</span><span style="color:var(--sub);font-size:11.5px">'+esc(String(e.ts||'').slice(5,16))+'</span></div>').join('');
    } else {
      wh+='<div class="wrow" style="color:var(--sub)"><span>尚未解锁（武术家 / 梦境旅人 / 大明星）</span><span></span></div>';
    }
    $('#watchBox').innerHTML=wh;
  }
  if(!planDirty) planBuildEdit(d.values||{});
}
async function refreshPlan(){ try{ renderPlan(await j('/api/plan')); }catch(e){} }
const _planBtn=document.getElementById('btnPlanSave');
if(_planBtn) _planBtn.onclick=async()=>{
  const g=id=>{const el=$(id);return el?(parseInt(el.value||'0',10)||0):0;};
  const updates={'力':g('#pn1'),'智':g('#pn2'),'魅':g('#pn3'),'工分':g('#pn4'),'金币':g('#pn5'),
    '初级毕业':$('#swPrim')?$('#swPrim').classList.contains('on'):false,
    '中级毕业':$('#swMid')?$('#swMid').classList.contains('on'):false};
  try{
    const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})});
    const d=await r.json();
    if(d.rejected&&d.rejected.length){$('#planMsg').className='saveMsg err';$('#planMsg').textContent='部分未保存：'+d.rejected.join('；');}
    else{planDirty=false;$('#planMsg').className='saveMsg';$('#planMsg').textContent='✅ 已保存';refreshPlan();}
  }catch(e){$('#planMsg').className='saveMsg err';$('#planMsg').textContent='保存失败：'+e.message;}
};
const _planSync=$('#btnPlanSync');
if(_planSync) _planSync.onclick=async()=>{
  if(_planSync.disabled) return;
  _planSync.disabled=true;
  const old=_planSync.textContent;
  _planSync.textContent='识别中…（约20秒）';
  $('#planMsg').className='saveMsg';
  $('#planMsg').textContent='正在识别游戏里的属性…';
  try{
    const r=await fetch('/api/plan/sync',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    const s=d.sync||{};
    if(s.ok){planDirty=false;$('#planMsg').className='saveMsg';$('#planMsg').textContent='✅ 已读取：力量'+s['力']+' · 智力'+s['智']+' · 魅力'+s['魅'];}
    else{$('#planMsg').className='saveMsg err';$('#planMsg').textContent='识别失败：'+(s.reason||'未知')+'（游戏画面忙，可稍后重试）';}
    refreshPlan();
  }catch(e){$('#planMsg').className='saveMsg err';$('#planMsg').textContent='识别失败：'+e.message;}
  _planSync.disabled=false;
  _planSync.textContent=old;
};
async function runnerAction(kind){
  const msg=$('#runnerMsg'); if(!msg)return;
  msg.className='saveMsg';
  msg.textContent=(kind==='start'?'正在启动调度器（连接设备约需几秒）…':'正在停止调度器（等当前任务收尾）…');
  try{
    const r=await fetch('/api/runner/'+kind,{method:'POST'});
    const d=await r.json();
    msg.textContent=d.msg||(d.ok?'完成':'失败');
    if(!d.ok) msg.className='saveMsg err';
  }catch(e){ msg.className='saveMsg err'; msg.textContent='请求失败：'+e.message; }
  refreshData();
}
const _rbStart=$('#btnRunnerStart'), _rbStop=$('#btnRunnerStop');
if(_rbStart) _rbStart.onclick=()=>runnerAction('start');
if(_rbStop) _rbStop.onclick=()=>{ if(confirm('停止调度器？正在进行的任务会先收尾再退出（约几秒到十几秒）。')) runnerAction('stop'); };
async function refreshAdventure(){
  try{ renderAdventure(await j('/api/adventure'+(window.__advDate?'?date='+encodeURIComponent(window.__advDate):''))); }catch(e){}
}

async function refreshLogs(){
  try{
    const d=await j('/api/logs?tail=250');
    const box=$('#logbox');
    const near=box.scrollHeight-box.scrollTop-box.clientHeight<48;
    let lines=d.lines||[];
    if(logFilter) lines=lines.filter(l=>l.indexOf(logFilter)>=0);
    box.textContent=lines.join('\n');
    // 元信息放日志框下方注脚（.logfoot），不再挤顶栏标题
    $('#logMeta').textContent=d.name?(d.name+' · '+lines.length+' 行'):'';
    if(logAuto&&near) box.scrollTop=box.scrollHeight;
  }catch(e){}
}

// ---- 日志页子页：实时日志（全部输出） / 收益记录（每次学习·打工结算） ----
// 历史模型与设置页二级完全一致（navDepth：总览 0 / 内页 1 / 二级 2）：
//   进收益记录 pushState（1→2）、点"实时日志"或侧滑 history.back() 退栈（2→1）。
// **"返回"类操作绝不能 pushState**——按钮压栈、手势退栈方向相反，会退不回去
// （设置页踩过：一条 6 页路径侧滑要退 5 次，见 INTEGRATION.md 第十二轮）。
function showLogIndex(skipHistory){
  const a=document.getElementById('logIndex'), b=document.getElementById('logReward');
  if(a) a.classList.remove('hide');
  if(b) b.classList.add('hide');
  window.__logSub=null;
  const t=document.getElementById('logTitle'); if(t) t.textContent='实时日志';
  document.querySelectorAll('#logSubtabs button')
    .forEach(x=>x.classList.toggle('on', x.dataset.lsub==='index'));
  if(skipHistory) return;
  if(navDepth>=2){ navDepth=1; try{ history.back(); }catch(e){} }   // 退栈，不是再压一条
}
function openLogReward(skipHistory){
  const a=document.getElementById('logIndex'), b=document.getElementById('logReward');
  if(!a||!b) return;
  // 已经在收益记录子页（再点一次切换条/定时刷新）不能重复压栈
  if(!skipHistory && window.__logSub==='reward') return;
  if(!skipHistory){
    try{ history.pushState({tab:'log',sub:'reward'}, '', '?tab=log&sub=reward'); }catch(e){}
    navDepth=2;
  }
  a.classList.add('hide'); b.classList.remove('hide');
  window.__logSub='reward';
  const t=document.getElementById('logTitle'); if(t) t.textContent='收益记录';
  document.querySelectorAll('#logSubtabs button')
    .forEach(x=>x.classList.toggle('on', x.dataset.lsub==='reward'));
  refreshRewards();
}
document.querySelectorAll('#logSubtabs button').forEach(b=>b.onclick=()=>{
  if(b.dataset.lsub==='reward') openLogReward(); else showLogIndex();
});

const RW_ICON={school:'/qp-icons/official/study_book.png', work:'/qp-icons/official/work_coin.png'};
const RW_NAME={school:'学习', work:'打工'};
async function refreshRewards(){
  try{
    const d=await j('/api/rewards'+(window.__rwDate?'?date='+encodeURIComponent(window.__rwDate):''));
    renderRewards(d);
  }catch(e){}
}
function rwAttrs(a){ return ['力量','智力','魅力'].filter(k=>a&&a[k]).map(k=>k+'+'+a[k]).join(' '); }
function renderRewards(d){
  window.__rw=d;
  const meta=$('#rwMeta'), sum=$('#rwSum'), list=$('#rwList'), row=$('#rwDateRow');
  // 没记录时把"统计范围/收益汇总"两个空白卡收起来，只留按次记录里的说明
  const sec=(id,v)=>{const e=document.getElementById(id); if(e) e.classList.toggle('hide', v);};
  if(!d||!d.ok){
    sec('rwScopeSec', true); sec('rwSumSec', true);
    if(meta) meta.textContent='暂无记录';
    if(sum) sum.innerHTML='';
    if(row) row.innerHTML='';
    if(list) list.innerHTML='<div class="empty">还没有收益记录。调度器跑完一节学习/一次打工、'
      +'检测到结算页后就会自动出现在这里（记录从本次更新之后开始）。</div>';
    return;
  }
  sec('rwScopeSec', false); sec('rwSumSec', false);
  // 统计范围下拉（与冒险页同款：今天 / 昨天 / 各天 / 全部）
  if(row){
    const sig=(d.date||'')+'|'+(d.dates||[]).join(',');
    if(row.__sig!==sig){
      row.__sig=sig;
      const opts=[];
      for(const dt of (d.dates||[])){
        const lab=dt===d.today?'今天':(dt===d.yesterday?'昨天':dt.slice(5).replace('-','/'));
        opts.push('<option value="'+dt+'"'+(dt===d.date?' selected':'')+'>'+lab+'</option>');
      }
      opts.push('<option value="all"'+(d.date==='all'?' selected':'')+'>全部</option>');
      // 只放选择器本身：卡上方已有灰色小标题"统计范围"，卡内不重复写一遍
      row.innerHTML='<select id="rwDate">'+opts.join('')+'</select>';
    }
  }
  const s=d.school||{}, w=d.work||{};
  const line=(ico,k,v,det,cls)=>'<div class="rwline"><img class="rwico" src="'+ico+'" alt="">'
    +'<span class="rwk">'+k+'</span><span class="rwv '+(cls||'')+'">'+v+'</span>'
    +(det?'<span class="rwd">'+det+'</span>':'')+'</div>';
  // 打工金币/工分解析不出时明说，别拿 +0 冒充"这次打工没收益"；
  // 部分解析出来时标出还有几次没解析
  const miss=(a,b)=>((a||0)<(b||0))?('（'+(b-a)+' 次未解析）'):'';
  const wc=(w.coins_n||0)>0?('金币 +'+(w.coins||0)+miss(w.coins_n,w.sessions))
                           :(w.sessions?'金币 —'+miss(0,w.sessions):'—');
  const wpc=(w.workpoints_n||0)>0?(' · 工分 +'+(w.workpoints||0)):'';
  // 广告加成（结算页「看视频获得 N 金币」）：单独一笔，不计进上面的金币
  const wad=(w.ad_coins_n||0)>0?(' · 看视频 +'+(w.ad_coins||0)):'';
  const sc=(s.credits_n||0)>0?(s.credits||0):null;
  let html='';
  html+=line(RW_ICON.school,'学习',(s.sessions||0)+' 节',
    (sc===null?(s.sessions?'学分 —'+miss(0,s.sessions):'学分 +0')
              :('学分 +'+sc+miss(s.credits_n,s.sessions)))
    +(rwAttrs(s.attrs)?(' · '+rwAttrs(s.attrs)):''),
    s.tired?'tired':'');
  html+=line(RW_ICON.work,'打工',(w.sessions||0)+' 次', wc+wpc+wad, w.tired?'tired':'');
  const tired=(s.tired||0)+(w.tired||0);
  if(tired){
    // 疲惫单独一行提示（"收益减少"是结算页原文，鼠标悬停看口径说明）。
    // 占位 span 与图标同宽 —— 否则这一行没有图标，标签会往左错一整列
    html+='<div class="rwline" title="结算页显示「疲惫，收益减少」的场次">'
      +'<span class="rwphs"></span><span class="rwk">提示</span>'
      +'<span class="rwv tired">疲惫 '+tired+' 次·收益减少</span></div>';
  }
  $('#rwSum').innerHTML=html;
  const tsLab=d.date==='all'?1:0;
  const arr=(d.recent||[]).slice().reverse();
  list.innerHTML=arr.map(r=>{
    const ts=r[0], kind=r[1], title=r[2], credits=r[3], attrs=r[4], coins=r[5],
          tired=r[6], wp=r[7], pay=r[8];
    const isS=kind==='school';
    const val=isS?(credits!=null?'学分+'+credits:'—')
                 :(coins!=null?'+'+coins:'—');
    // 打工行把工分也带上；工资构成（本金/雇佣加成）挂在金币上的悬停提示里
    const mid=[esc(title||''), esc(attrs||''), (!isS&&wp!=null?'工分+'+wp:''),
               (tired?'<span class="tired">疲惫</span>':'')]
      .filter(Boolean).join(' · ');
    return '<div class="arow">'
      +'<span class="ai"><img src="'+(RW_ICON[kind]||'')+'" alt=""><span class="rk">'
      +(tsLab?esc(ts):esc((ts||'').slice(6)))+'</span></span>'
      +'<span class="ag">'+mid+'</span>'
      +'<span class="av '+((coins!=null&&!isS)?'pos':'')+(val==='—'?' zero':'')+'"'
      +(pay?' title="'+esc(pay)+'"':'')+'>'+val+'</span>'
      +'</div>';
  }).join('')||'<div class="empty">这一天还没有收益记录</div>';
  if(meta){
    const scope=d.date==='all'?'全部历史':(d.date===d.today?'今天':d.date.slice(5).replace('-','/'));
    const tn='今日 学习 '+(d.today_school?.sessions||0)+' 节 / 打工 '
      +(d.today_work?.sessions||0)+' 次';
    // 宠物名/主人名（结算页头部解析出来的）放最前：换宠物或换号时一眼看出这份数据是谁的
    const who=d.pet?(d.pet+(d.owner?(' · '+d.owner):'')):'';
    meta.textContent=(who?who+' · ':'')+scope+' · 共 '+(d.n||0)+' 次 · '
      +(d.date===d.today?'':tn+' · ')+'更新 '+(d.updated||'');
  }
}
document.addEventListener('change',function(e){
  if(e.target&&e.target.id==='rwDate'){ window.__rwDate=e.target.value; refreshRewards(); }
});

// 秒级：时钟 + 倒计时
setInterval(()=>{
  const n=new Date();
  if(etaRemain!=null && schedOn){
    etaRemain-=1;
    const sub= etaRemain>0? ('剩余 '+hms(etaRemain)+' · 结束后自动开启下一项') : '收尾中…';
    const el=$('#workSub'); if(el) el.textContent=sub;
  }
},1000);

$('#btnAuto').className=logAuto?'on':'';
$('#btnAuto').onclick=()=>{logAuto=!logAuto;$('#btnAuto').className=logAuto?'on':'';try{localStorage.setItem('qpet_logAuto',logAuto?'1':'0')}catch(e){}};
$('#logFilter').oninput=e=>{logFilter=e.target.value.trim();refreshLogs()};

function renderCfg(rows){
  const HIDE=['调度策略','打工','金币阈值','合计停止点','踩踩','PK','冒险','护理'];
  rows=(rows||[]).filter(r=>HIDE.indexOf(r[0])<0);
  if(!rows.length){$('#cfgList').innerHTML='';return}
  $('#cfgList').innerHTML=rows.map(r=>'<div class="row"><span class="k">'+r[0]+'</span><span class="v">'+r[1]+'</span></div>').join('');
}

// ---- 设置页两级切换：一级分类列表 <-> 二级分类详情 ----
// **二级是独立的一层历史**（导航层级：总览 0 / 内页 1 / 设置二级 2）：
//   进二级 pushState（navDepth 1 -> 2），"返回设置列表"/侧滑 都是 **history.back()**（退栈）。
// 改前每进一次二级、每点一次"返回设置列表"都 pushState（按钮也压栈），
// 于是侧滑会反向往二级里钻（实测：进二级 -> 点"返回设置列表" -> 侧滑 又弹回二级）。
// skipHistory=true 只用于"定时刷新重建表单后恢复层级"，不碰历史。
function showSetIndex(skipHistory){
  const a=document.getElementById('setIndex'), b=document.getElementById('setDetail');
  if(a) a.classList.remove('hide');
  if(b) b.classList.add('hide');
  window.__setGrp=null;
  if(skipHistory) return;
  // 从二级返回一级：**退栈**（不是再压一条一级，否则侧滑会退回二级）
  if(navDepth>=2){ navDepth=1; try{ history.back(); }catch(e){} }
}
function openSetGroup(key, skipHistory){
  const a=document.getElementById('setIndex'), b=document.getElementById('setDetail');
  if(!a||!b) return;
  const grp=document.getElementById('grp_'+key);
  if(!grp) return;
  if(!skipHistory){
    try{ history.pushState({tab:'set',grp:key}, '', '?tab=set&grp='+key); }catch(e){}
    navDepth=2;
  }
  // 只显示这一组，其余隐藏
  document.querySelectorAll('#setForm .fgrp').forEach(g=>g.classList.toggle('hide', g.id!=='grp_'+key));
  const t=document.getElementById('setDetailTitle');
  if(t) t.textContent=grp.dataset.title||'设置';
  a.classList.add('hide'); b.classList.remove('hide');
  window.__setGrp=key;
}
document.addEventListener('click',function(e){
  const b=e.target.closest && e.target.closest('#btnSetBack');
  if(b){ showSetIndex(); }
},true);

// ---- 共用卡片生成器（设置页与通知页共用，保证两页排版完全一致） ----
// 结构：.fgrp（小节）> .fsect（卡外小标题） + .fsec（白卡，内含 .frow 行）
function card(title, rows, key){
  return '<div class="fgrp"'+(key?(' id="grp_'+key+'" data-title="'+title+'"'):'')
    +'><div class="fsect">'+title+'</div>'
    +'<div class="fsec">'+rows.join('')+'</div></div>';
}
// 行：左键值 + 右侧任意控件（开关/输入/下拉），设置页与通知页通用
function rowKv(label, ctrl, tip){
  return '<div class="frow"'+(tip?(' title="'+tip+'"'):'')+'>'
    +'<span class="k">'+label+'</span>'+ctrl+'</div>';
}

let setInit=null, setDirty=false;

// 表单"正忙"判定：避免定时刷新重建表单把用户正在操作的控件销毁。
// 现象：下拉/选择框点开后几秒被自动关闭、输入框失焦（每 6s 的 refreshData 重建表单）。
let _lastFormTouch = 0;
function formBusy(){
  const ae = document.activeElement;
  // 焦点在设置/通知表单内（含 select/input/button）
  if(ae && ae.closest && ae.closest('#setForm, #notifyForm')) return true;
  // 刚有过交互（2 秒保护期）—— 覆盖"点了下拉但焦点已转移"的瞬间
  return (Date.now() - _lastFormTouch) < 2000;
}
document.addEventListener('pointerdown', e=>{
  if(e.target.closest && e.target.closest('#setForm, #notifyForm')) _lastFormTouch = Date.now();
}, true);
document.addEventListener('focusin', e=>{
  if(e.target.closest && e.target.closest('#setForm, #notifyForm')) _lastFormTouch = Date.now();
}, true);
// 好友名单（由 /api/data 的 friends 字段带入），供各表单的下拉选择
let FRIENDS = [];
// 生成"可输入下拉"：既可从已有好友里选，也保留手输能力（用 datalist）
// 为什么用 datalist：原生、无依赖、iOS Safari 支持良好，且不破坏现有
// "读 input.value 保存"的逻辑（id/name 不变，保存代码零改动）。
function friendPicker(id, cur, placeholder){
  // 用原生 <select> 让用户直接选（iOS Safari 对 datalist 支持差：
  // 只在键盘上方出建议条，观感仍是"输入框"）。
  // 结构：select（选已有好友 / 手动输入）+ input（保留原 id，存实际值）。
  // input 保持原 id 不变 -> 保存逻辑（读取该 input 的 value）零改动。
  const val = cur||'';
  const opts = FRIENDS.map(n=>'<option value="'+esc(n)+'"'
      + (n===val?' selected':'')+'>'+esc(n)+'</option>').join('');
  const isCustom = val && FRIENDS.indexOf(val)<0;
  // 下拉里的是【好友列表的主人昵称】（content-desc 抓取，准确）；
  // 宠物名不在列表里 —— 用「手动输入…」填。
  const cnt = FRIENDS.length;
  return '<span class="fpick">'
    + '<select class="fpsel" data-for="'+id+'"'
      + (cnt?'':' title="还没有好友名单：跑一次踩踩/福袋后自动生成"')+'>'
      + '<option value="">（未设置）</option>'
      + (cnt?('<optgroup label="好友昵称（'+cnt+'）">'+opts+'</optgroup>')
            :'<option value="" disabled>（暂无好友名单）</option>')
      + '<option value="__custom__"'+(isCustom?' selected':'')+'>手动输入宠物名/昵称…</option>'
    + '</select>'
    + '<input type="text" id="'+id+'" class="fpinput'+(isCustom?'':' hide')+'"'
      + ' autocomplete="off" placeholder="'+esc(placeholder||'输入宠物名或主人昵称')+'"'
      + ' value="'+esc(val)+'">'
  + '</span>';
}
// 下拉选择后同步到 input（并触发自动保存）
document.addEventListener('change', function(e){
  const sel=e.target.closest && e.target.closest('.fpsel');
  if(!sel) return;
  const inp=document.getElementById(sel.dataset.for);
  if(!inp) return;
  const v=sel.value;
  if(v==='__custom__'){ inp.classList.remove('hide'); inp.focus(); return; }
  inp.classList.add('hide');
  inp.value=v;
  inp.dispatchEvent(new Event('input',{bubbles:true}));   // 触发改动即保存
}, true);

function renderSettings(ed){
  if(!ed) return;
  setInit=Object.assign({},ed);
  const sel=(id,opts,cur)=>'<select id="'+id+'">'+opts.map(v=>'<option value="'+v+'"'+(v===cur?' selected':'')+'>'+v+'</option>').join('')+'</select>';
  // 选项名只描述 work 与 adventure 的先后（school/hire_friend 两组里都固定在前，
  // 且各任务能否执行还取决于自身条件——金币/时长上限/疲劳/次数，见 _school_due 等）
  const moOpts=[['school>hire_friend>work>adventure','先打工，打满 8h 再冒险'],['school>hire_friend>adventure>work','先冒险，冒险没次数了再打工']];
  const moSel=(cur)=>'<select id="selMainOrder" title="主任务组（学习/雇佣/冒险/打工）互斥时的执行优先级：按 > 顺序逐个检查，第一个条件满足的执行。学习与雇佣好友在两组预设里都固定排在最前，此处切换的只是打工与冒险的先后；每个任务还要自身条件满足才会执行（金币达标/未超时长上限/未疲劳/次数未满），改完下一轮调度生效">'+moOpts.map(o=>'<option value="'+o[0]+'"'+(o[0]===cur?' selected':'')+'>'+o[1]+'</option>').join('')+(moOpts.some(o=>o[0]===cur)?'':'<option value="'+esc(cur||'')+'" selected>自定义：'+esc(cur||'')+'</option>')+'</select>';
  // 复用全局 card()：组标题在卡片【外】，行在白色卡片【内】
  const FG=(t,rows,key)=>card(t,rows,key);
  // 「合计停止点」说明表：只有 3 行 2 列，随配置动态生成（用户嫌原来的
  // 「当前设置 / 收益档 / 说明」三段长文太啰嗦，要求"弄个表格放在下面"）。
  const stopExplain=(e)=>{
    const t1=Number(e.efficiency_tier1_hours??8), stop=Number(e.stop_total_hours??12);
    const rows=[];
    if(t1>0) rows.push(['没到 '+t1+' 小时','正常收益 100%']);
    if(t1>0&&(stop<=0||t1<stop)) rows.push(['满 '+t1+' 小时','收益降到 25%（只提示，继续跑）']);
    rows.push(stop>0
      ? ['满 '+stop+' 小时','学习和打工<b>一起停</b>，转冒险']
      : ['已设 0 = 不限','不会按时长自动停']);
    if(stop>0) rows.push(['次日 0 点','时长清零，重新开始']);
    return '<div class="frow" style="display:block"><table class="mintbl">'
      +'<tr><th>什么时候</th><th>会发生什么</th></tr>'
      +rows.map(r=>'<tr><td>'+r[0]+'</td><td>'+r[1]+'</td></tr>').join('')
      +'</table></div>';
  };
  // 只有三项底层停止线真不一致（手改过 config.yaml）时才多一行提示，平时不占地方
  const stopWarn=(e)=>{
    const d=Number(e.daily_hour_limit||0), w=Number(e.work_stop_hours||0),
          t=Number(e.efficiency_tier2_hours||0);
    if(d===w&&w===t) return '';
    return '<div class="frow" style="display:block;color:var(--warn);font-size:12px">'
      +'⚠ 配置里三项停止线不一致（停学习 '+d+' / 停打工 '+w+' / 全停 '+t
      +'）——改上面的数会把三项一起写成同一个值</div>';
  };
  $('#setForm').innerHTML=
    FG('学习',[
    '<div class="frow"><span class="k">启用学习</span><button class="sw'+(ed.school_enabled?' on':'')+'" id="swSchool" title="开=学习+打工（默认）；关=只打工不学习。与「调度」页的「学习」勾选框是同一个开关"></button></div>',
    '<div class="frow"><span class="k">学习科目</span>'+sel('selSchoolAttr', ['力量','智力','魅力','夏令营'], ed.school_attribute)+'</div>',
    '<div class="frow"><span class="k">每天学习次数</span><input type="number" id="numSchoolTimes" min="0" step="1" title="0=不限" value="'+(ed.school_times??0)+'"></div>',
    '<div class="frow"><span class="k">课时档位</span>'+sel('selSchoolDur', ['短课','长课'], ed.school_duration)+'</div>',
    '<div class="frow"><span class="k">当前选择</span><span id="schoolHint" style="color:var(--sub);font-size:12px"></span></div>',
    '<div class="frow"><span class="k">档位说明</span><span style="color:var(--sub);font-size:12px">课程轮播固定 7 张：卡1-3 短课（力量/智力/魅力）、卡4-6 长课（同序）、卡7 萌芽夏令营。各学院具体分钟数不同（初级10/30、高级30/90），实际时长选课后从面板自动读取，升级学院不用改配置</span></div>',
    '<div class="frow"><span class="k">课时说明</span><span style="color:var(--sub);font-size:12px">短课单位消耗收益更高（每30分钟 +6属性/+30学分 vs 长课 +5/+25）</span></div>',
    ],'school')+
    FG('打工',[
    '<div class="frow"><span class="k">打工地点</span>'+sel('selLoc', ed.work_locations||[], ed.work_location)+'</div>',
    '<div class="frow"><span class="k">打工时长</span>'+sel('selDur', ['10分钟','45分钟','2小时'], ed.work_duration)+'</div>',
    '<div class="frow"><span class="k">优先雇佣</span>'+friendPicker('txtHire', ed.hire_name, '宠物名/主人名，空=自动选收益最高')+'</div>',
    '<div class="frow"><span class="k">等TA空闲</span><button class="sw'+(ed.hire_wait?' on':'')+'" id="swHireWait" title="开=优先雇佣的好友正在打工/学习（面板显示 出门中/被雇佣中）时不换人，点头像进主页读剩余时间，等到他结束再雇（期间先跑冒险/护理等其他任务）；显示 对方今天很累了 时等待无意义，仍换收益最高的人。需先填「优先雇佣」"></button></div>',
    ],'work')+
    FG('学习 / 打工 配额（各自上限，可选）',[
    '<div class="frow"><span class="k">今日学习</span><input type="number" id="numStudyQuota" min="0" max="24" step="1" title="今天最多学几小时，按【学习自身】时长算。0 = 今天不学习；24 = 不各自限制（收工只看下面的「合计满则停止」）" value="'+(ed.study_quota_hours??8)+'"><span class="u">小时</span></div>',
    '<div class="frow"><span class="k">今日打工</span><input type="number" id="numWorkQuota" min="0" max="24" step="1" title="今天最多打几小时，按【打工自身】时长算。0 = 今天不打工；24 = 不各自限制（收工只看下面的「合计满则停止」）" value="'+(ed.work_quota_hours??8)+'"><span class="u">小时</span></div>',
    '<div class="frow"><span class="k">金币阈值</span><input type="number" id="numCoin" min="0" step="100" title="金币 ≥ 该值优先学习，低于该值先打工赚够再学。只学习时请填 0，否则金币不足会先去打工" value="'+(ed.coin_threshold??'')+'"></div>',
    '<div class="frow"><span class="k">当前设置</span><span id="quotaHint" style="color:var(--sub);font-size:12px"></span></div>',
    '<div class="frow"><span class="k">一键预设</span><span style="display:flex;gap:6px;flex-wrap:wrap">'
      +'<button class="minibtn" data-quota="study8" title="学习8 / 打工0 / 合计满8全停 / 金币0">只学习 8h</button>'
      +'<button class="minibtn" data-quota="study12" title="学习12 / 打工0 / 合计满12全停 / 金币0">只学习 12h</button>'
      +'<button class="minibtn" data-quota="work8" title="学习0 / 打工8 / 合计满8全停">只打工 8h</button>'
      +'<button class="minibtn" data-quota="half" title="学习4 / 打工4 / 合计满8全停 / 金币2000">各半 4+4</button>'
      +'<button class="minibtn" data-quota="both" title="学习8 / 打工8 / 合计满8全停 / 金币2000（默认：按金币自动选）">都行 8+8</button>'
      +'</span></div>',
    ],'quota')+
    FG('合计停止点与收益档（按学习+打工合计）',[
    '<div class="frow"><span class="k">合计满则停止</span><input type="number" id="numStopTotal" min="0" max="24" step="1" title="学习+打工合计达到该时长后，学习和打工【一起停】（剩下的时间跑冒险/支线），次日 0 点清零恢复。0 = 不限。不区分学习/打工——保存时三处判定（停学习/停打工/全停）一起写成这个数" value="'+(ed.stop_total_hours??12)+'"><span class="u">小时 · 学习+打工一起停</span></div>',
    stopExplain(ed),
    stopWarn(ed),
    ],'fatigue')+
    FG('调度',[
    '<div class="frow"><span class="k">主任务优先级</span>'+moSel(ed.main_order)+'</div>',
    ],'schedule')+
    FG('踩踩',[
    '<div class="frow"><span class="k">踩踩次数/天</span><input type="number" id="numVisit" min="0" step="1" value="'+(ed.visit_times??'')+'"></div>',
    ],'visit')+
    FG('PK',[
    '<div class="frow"><span class="k">PK 次数/天</span><input type="number" id="numPk" min="0" step="1" value="'+(ed.pk_times??'')+'"></div>',
    '<div class="frow"><span class="k">PK 只打</span>'+friendPicker('txtPkOnly', ed.pk_only, '昵称或宠物名，逗号分隔，空=不限')+'</div>',
    '<div class="frow"><span class="k">PK 跳过</span>'+friendPicker('txtPkSkip', ed.pk_skip, '昵称或宠物名，逗号分隔，空=不跳过')+'</div>',
    '<div class="frow"><span class="k">PK 打手</span>'+friendPicker('txtPkHelper', ed.pk_helper, '只雇这些宠物代打（逗号分隔，按优先序）')+'</div>',
    '<div class="frow"><span class="k">打手兜底</span><button class="sw'+(ed.pk_helper_fallback?' on':'')+'" id="swPkHf" title="开=名单里的打手都不可雇（被雇佣中/不可雇佣/已达上限）时，自动雇战力最高的可雇宠物"></button></div>',
    '<div class="frow"><span class="k">PK 等级上限</span><input type="number" id="numPkLv" min="-2" step="1" title="-1=只打比我低；-2=只打比打手低" value="'+(ed.pk_max_level??0)+'"></div>',
    '<div class="frow"><span class="k">等级过滤说明</span><span style="color:var(--sub);font-size:12px">0=不限；-1=只打比我低的；-2=只打比打手低的</span></div>',
    ],'pk')+
    FG('冒险',[
    '<div class="frow"><span class="k">冒险次数/天</span><input type="number" id="numAdv" min="0" step="1" title="0=不冒险；主号策略设 999 ≈ 不限（疲劳后全冒险）" value="'+(ed.adventure_times??'')+'"></div>',
    '<div class="frow"><span class="k" title="附近走走约 45 秒（靠连跑刷次数，配大次数用）/ 诗和远方约 2 小时（单次时间长）">冒险类型</span>'+sel('selAdvType', ['附近走走','诗和远方'], ed.adventure_type)+'</div>',
    ],'adventure')+
    FG('护理',[
    '<div class="frow"><span class="k" title="低于该值就喂食/洗澡（本项目的触发线）。官方「一键护理」的口径是「体力、清洁补至 100，心情同步提升」，点道具那档是补至 80">护理阈值（体力/清洁）</span><span class="two"><input type="number" id="numEnergy" min="0" max="100" value="'+(ed.care_energy??'')+'"><input type="number" id="numClean" min="0" max="100" value="'+(ed.care_clean??'')+'"></span></div>',
    '<div class="frow"><span class="k" title="一键护理=点主页的官方按钮（官方文案：体力、清洁补至 100，心情同步提升；点道具档补至 80），点完清空状态缓存；ocr检测=读状态面板，按上面的阈值手动喂食/洗澡">护理方式</span>'+sel('selCare', ['一键护理','ocr检测'], ed.care_method)+'</div>',
    '<div class="frow"><span class="k">补货数量（个）</span><input type="number" id="numExchange" min="1" max="99" step="1" title="饼干/香皂不足时一次金币买多少个" value="'+(ed.care_exchange??'')+'"></div>',
    '<div class="frow"><span class="k">检查间隔（秒）</span><input type="number" id="numCareInt" min="10" step="10" title="每隔这么久检查一次体力/清洁，不足则喂食/洗澡" value="'+(ed.care_interval??60)+'"></div>',
    ],'care')+
    FG('好友护理',[
    '<div class="frow"><span class="k">好友护理</span><button class="sw'+(ed.friend_care_enabled?' on':'')+'" id="swFC" title="开=按间隔到指定好友家护理（体力/清洁<90自动补）"></button></div>',
    '<div class="frow"><span class="k">好友护理对象</span>'+friendPicker('txtFCName', ed.friend_care_name, '宠物名或主人名')+'</div>',
    '<div class="frow"><span class="k">好友护理时间段</span><input type="text" id="txtFCRange" style="width:calc(var(--u) * 110)" title="HH:MM-HH:MM，支持跨零点。起止相同（00:00-00:00）= 跨零点 = **全天**" value="'+esc(ed.friend_care_range||'00:00-00:00')+'"></div>',
    '<div class="frow"><span class="k">好友护理间隔（秒）</span><input type="number" id="numFCInt" min="30" step="30" value="'+(ed.friend_care_interval??'')+'"></div>',
    '<div class="frow"><span class="k">好友护理方式</span>'+sel('selFCMethod', ['ocr检测','一键护理'], ed.friend_care_method)+'</div>',
    ],'friend_care')+
    FG('雇佣好友',[
    '<div class="frow"><span class="k">雇佣好友</span><button class="sw'+(ed.hire_friend_enabled?' on':'')+'" id="swHF" title="开=按间隔去好友家雇佣（帮他打工）。与「调度」页的「雇佣好友」勾选框是同一个开关"></button></div>',
    '<div class="frow"><span class="k">每天次数</span><input type="number" id="numHFTimes" min="0" step="1" title="每天最多雇佣几次，0 = 不雇佣" value="'+(ed.hire_friend_times??8)+'"></div>',
    '<div class="frow"><span class="k">目标好友</span><span style="color:var(--sub);font-size:12px;line-height:1.6">'
      +'跟随「打工 → 优先雇佣」：那里填谁就优先去谁家，找不到时依次退到备选名单'
      +'（config.yaml 的 <b>hire_friend.friend_name</b>，逗号分隔多个）。留空则不雇佣。</span></div>',
    ],'hire_friend')+
    FG('被雇佣（帮好友打工）',[
    '<div class="frow"><span class="k">被雇佣托管</span><button class="sw'+(ed.employed_enabled?' on':'')+'" id="swEmp" title="开=定时出门检查是否被好友雇去打工"></button></div>',
    '<div class="frow"><span class="k">被雇佣处理</span>'+sel('selEmpAction', ['等到25/75（小于45min）','等到25/75','立刻召回','让利雇主（不召回）'], ed.employed_action)+'</div>',
    '<div class="frow"><span class="k">检查间隔（秒）</span><input type="number" id="numEmpInt" min="30" step="30" value="'+(ed.employed_interval??'')+'"></div>',
    ],'employed')+
    FG('福袋',[
    '<div class="frow"><span class="k">福袋领取</span><button class="sw'+(ed.gift_bag_enabled?' on':'')+'" id="swGiftBag" title="开=定时遍历好友领取系绳福袋"></button></div>',
    '<div class="frow"><span class="k">福袋时间段</span><input type="text" id="txtGbRange" style="width:calc(var(--u) * 110)" title="HH:MM-HH:MM，支持跨零点。00:00-00:00 = 全天" value="'+esc(ed.gift_bag_range||'00:00-00:00')+'"></div>',
    '<div class="frow"><span class="k">福袋扫描间隔（秒）</span><input type="number" id="numGbInt" min="60" step="60" value="'+(ed.gift_bag_interval??'')+'"></div>',
    ],'gift_bag')+
    FG('职业',[
    '<div class="frow"><span class="k">隐藏职业解锁监控</span><button class="sw'+(ed.career_watch?' on':'')+'" id="swCareer" title="开=每节课结算后读职业树；武术家/梦境旅人/大明星解锁时记录并推送通知"></button></div>',
    '<div class="frow"><span class="k">解锁后自动停学</span><button class="sw'+(ed.career_stop_study?' on':'')+'" id="swCareerStop" title="开=解锁时自动关闭学习任务（等你安排下一阶段）"></button></div>',
    '<div class="frow"><span class="k">兜底检查间隔（分钟）</span><input type="number" id="numCareerInt" min="0" step="10" title="0 = 只每节课后检查" value="'+(ed.career_interval??60)+'"></div>',
    ],'career')+
    FG('连接手机（ADB）',[
    '<div class="frow"><span class="k">adb 路径</span><input type="text" id="txtAdbPath" style="width:100%" placeholder="留空自动探测（PATH / Homebrew / Android SDK）" value="'+esc(ed.adb_path||'')+'"></div>',
    '<div class="frow"><span class="k">设备序列号</span><input type="text" id="txtAdbSerial" style="width:100%" placeholder="留空 = 用第一台在线设备" value="'+esc(ed.adb_serial||'')+'"></div>',
    '<div class="frow" style="display:block"><span style="color:var(--sub);font-size:12px;line-height:1.6">'
      +'这两项属于<b>连接层</b>，改完要<b>重启调度器</b>才生效（连接在调度器启动时建立）。'
      +'设备序列号也可以从下面列表里直接选。</span></div>',
    '<div class="frow"><span class="k">在线设备</span><button class="minibtn" id="btnAdbRefresh">刷新</button></div>',
    '<div class="frow" style="display:block"><div id="adbDevices" style="font-size:12px;color:var(--sub);line-height:1.8">点「刷新」查看当前设备</div></div>',
    '<div class="frow"><span class="k">连接地址</span><input type="text" id="txtAdbAddr" style="width:100%" placeholder="192.168.1.5:5555 / 127.0.0.1:7555（省略端口按 :5555）"></div>',
    '<div class="frow"><span class="k">无线 / 模拟器</span><button class="minibtn" id="btnAdbConnect">连接</button></div>',
    '<div class="frow" style="display:block"><div id="adbMsg" style="font-size:12px;line-height:1.8"></div></div>',
    ],'adb');
  // 通知页单独渲染（不放设置页：渠道配置项多，独立成板更清楚）
  renderNotifyForm(ed);
  // ---- ADB 卡片：两个文本框跟着自动保存走，两个按钮各调一次接口 ----
  ['#txtAdbPath','#txtAdbSerial'].forEach(id=>{
    const el=$(id); if(el) el.addEventListener('change',()=>markDirtyAndSave());
  });
  { const b=$('#btnAdbRefresh'); if(b) b.onclick=refreshAdb; }
  { const b=$('#btnAdbConnect'); if(b) b.onclick=adbConnectNow; }
  $('#swSchool').onclick=()=>{ $('#swSchool').classList.toggle('on'); markDirtyAndSave(); };
  $('#swFC').onclick=()=>{ $('#swFC').classList.toggle('on'); markDirtyAndSave(); };
  $('#swEmp').onclick=()=>{ $('#swEmp').classList.toggle('on'); markDirtyAndSave(); };
  $('#swGiftBag').onclick=()=>{ $('#swGiftBag').classList.toggle('on'); markDirtyAndSave(); };
  $('#swHF').onclick=()=>{ $('#swHF').classList.toggle('on'); markDirtyAndSave(); };
  $('#swCareer').onclick=()=>{ $('#swCareer').classList.toggle('on'); markDirtyAndSave(); };
  $('#swCareerStop').onclick=()=>{ $('#swCareerStop').classList.toggle('on'); markDirtyAndSave(); };
  $('#swPkHf').onclick=()=>{ $('#swPkHf').classList.toggle('on'); markDirtyAndSave(); };
  $('#swHireWait').onclick=()=>{ $('#swHireWait').classList.toggle('on'); markDirtyAndSave(); };
  // 「当前设置」实时提示：把四个数字翻译成一句人话，避免填错组合（如只学习却
  // 忘了把金币阈值调 0 → 金币不足时会先去打工，看着像"没在学习"）
  const qv=id=>{const el=$(id); return el?parseInt(el.value,10):NaN;};
  const updQuotaHint=()=>{
    const el=$('#quotaHint'); if(!el) return;
    const sq=qv('#numStudyQuota'), wq=qv('#numWorkQuota');
    const stop=qv('#numStopTotal'), coin=qv('#numCoin');
    const parts=[];
    if(sq===0&&wq===0) parts.push('学习和打工都关了（只剩冒险/支线）');
    else if(sq>0&&wq===0) parts.push('只学习 '+sq+' 小时');
    else if(sq===0&&wq>0) parts.push('只打工 '+wq+' 小时');
    else if(sq>0&&wq>0) parts.push('学习 '+sq+'h + 打工 '+wq+'h，先到先切');
    if(stop>0) parts.push('合计满 '+stop+'h 学习+打工一起停');
    else parts.push('合计不限（不会自动收工）');
    if(sq>0&&wq===0&&coin>0) parts.push('⚠ 金币阈值 '+coin+' > 0：金币不足时会先去打工，想纯学习请设 0');
    el.textContent=parts.join('；');
    el.style.color=(sq>0&&wq===0&&coin>0)?'var(--warn)':'var(--sub)';
  };
  ['#numStudyQuota','#numWorkQuota','#numStopTotal','#numCoin'].forEach(id=>{
    const el=$(id); if(el) el.addEventListener('input',updQuotaHint);
  });
  updQuotaHint();
  // 「合计停止点」不再有独立提示行（原来的「当前设置/收益档/说明」三段被用户嫌太长，
  // 已换成卡片里那张 3 行说明表，见 stopExplain/stopWarn；表格随配置渲染，
  // 改完保存后 refreshData 重建表单会自动跟着变）
  // 「当前选择」实时提示：科目与档位是两个独立字段，选「夏令营」时档位会被忽略
  // （夏令营固定第 7 张卡、不按属性选框），这里说清，避免看着矛盾
  const updSchoolHint=()=>{
    const el=$('#schoolHint'); if(!el) return;
    const attrEl=$('#selSchoolAttr'), durEl=$('#selSchoolDur');
    const attr=attrEl?attrEl.value:'', dur=durEl?durEl.value:'';
    if(attr==='夏令营'){
      el.textContent='科目=萌芽夏令营（卡7）：随机属性+5，不走属性课卡；下面的课时档位对它无效';
      el.style.color='var(--warn)';
    } else {
      el.textContent=attr+' · '+(dur==='长课'?'长课（卡4-6）':'短课（卡1-3）')
        +' —— 具体分钟数选课后自动读取（各学院不同）';
      el.style.color='var(--sub)';
    }
  };
  ['#selSchoolAttr','#selSchoolDur'].forEach(id=>{
    const el=$(id); if(el) el.addEventListener('change',updSchoolHint);
  });
  updSchoolHint();
  // 一键预设：把「学习/打工怎么分」这类需求一次填好（只改表单，改动即自动保存）。
  // lim = 合计停止点（#numStopTotal），保存时三键同值 → 合计满 lim 小时两项一起停
  const QUOTA_PRESETS={
    study8:  {study:8,  work:0, lim:8,  coin:0},
    study12: {study:12, work:0, lim:12, coin:0},
    work8:   {study:0,  work:8, lim:8,  coin:2000},
    half:    {study:4,  work:4, lim:8,  coin:2000},
    both:    {study:8,  work:8, lim:8,  coin:2000},
  };
  document.querySelectorAll('[data-quota]').forEach(b=>{
    b.onclick=()=>{
      const p=QUOTA_PRESETS[b.dataset.quota]; if(!p) return;
      const set=(id,v)=>{const el=$(id); if(el) el.value=v;};
      set('#numStudyQuota',p.study); set('#numWorkQuota',p.work);
      set('#numStopTotal',p.lim); set('#numCoin',p.coin);
      markDirtyAndSave(); updQuotaHint();
      const msg=$('#saveMsg');
      if(msg){ msg.className='saveMsg'; msg.textContent='已应用「'+b.textContent+'」，自动保存中…'; }
    };
  });

  // ---- 一级：分类列表（按 tasks.order 的常见顺序排列）----
  // 一级：分组卡片（照 QQ 宠物设置页 —— 小标题在卡外，卡内多行带 › 箭头）
  const MENU=[
    ['核心任务',[['school','学习'],['work','打工'],['quota','学习/打工 配额'],['fatigue','合计停止点与收益档']]],
    ['日常互动',[['care','护理'],['friend_care','好友护理'],['hire_friend','雇佣好友'],['visit','踩踩'],['pk','PK'],['adventure','冒险']]],
    ['扩展',[['employed','被雇佣'],['gift_bag','福袋'],['career','职业']]],
    ['系统',[['schedule','调度'],['adb','连接手机（ADB）']]],
  ];
  const menu=$('#setMenu');
  if(menu){
    menu.innerHTML=MENU.map(([sec,items])=>{
      const rows=items.filter(([k])=>document.getElementById('grp_'+k))
        .map(([k,t])=>'<div class="frow menurow" data-grp="'+k+'">'
          +'<span class="k">'+t+'</span><span class="chev">›</span></div>').join('');
      if(!rows) return '';
      return '<div class="msec"><div class="fsect">'+sec+'</div><div class="fsec">'+rows+'</div></div>';
    }).join('');
    menu.querySelectorAll('.menurow').forEach(r=>r.onclick=()=>openSetGroup(r.dataset.grp));
  }
  // 重建后恢复原来的层级（定时刷新会重跑本函数，直接 showSetIndex 会把
  // 正在看二级详情的用户弹回一级 —— 曾实测每 6 秒被弹回一次）
  // 定时刷新重建：**只恢复视图层级，不碰历史**（skipHistory=true）
  // ?grp=<key> 直开某个二级分组（与 ?tab= 同理，便于分享链接/截图/调试）：
  // 每次渲染都读一次 URL，不能只看 window.__setGrp —— 首次渲染的时序不确定
  // （数据到达才渲染，实测依赖 __setGrp 会不生效）。点"返回一级"会清掉该参数。
  let want=window.__setGrp || _grpFromUrl;
  _grpFromUrl='';                        // 直开参数只用一次
  if(want && document.getElementById('grp_'+want)) openSetGroup(want, true);
  else showSetIndex(true);
}

// ---- 通知页（独立板块）：渠道配置 + 事件开关 + 测试 ----
// 与设置页共用 setInit（同一份 editable 快照），但有自己的保存按钮/提示，
// 只提交本页字段（差量），互不干扰。
function renderNotifyForm(ed){
  const form=$('#notifyForm'); if(!form) return;
  // 与设置页共用同一个 card() 生成器（原来是自己拼的，容易走样）
  const FGn=(t,rows)=>card(t,rows);
  form.innerHTML=
    FGn('飞书群机器人',[
    '<div class="frow"><span class="k">启用</span><button class="sw'+(ed.notify_feishu_enabled?' on':'')+'" id="swFeishu" title="开=用飞书自定义机器人推送"></button></div>',
    '<div class="frow"><span class="k">webhook</span><input type="text" id="txtFsHook" style="width:100%" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" value="'+esc(ed.notify_feishu_webhook)+'"></div>',
    '<div class="frow"><span class="k">加签密钥</span><input type="text" id="txtFsSecret" placeholder="安全设置选「签名校验」时必填，否则留空" value="'+esc(ed.notify_feishu_secret)+'"></div>',
    ])+
    FGn('Telegram Bot',[
    '<div class="frow"><span class="k">启用</span><button class="sw'+(ed.notify_telegram_enabled?' on':'')+'" id="swTg" title="开=用 Telegram Bot 推送"></button></div>',
    '<div class="frow"><span class="k">Bot Token</span><input type="text" id="txtTgToken" style="width:100%" placeholder="123456789:AAE…（@BotFather 获取）" value="'+esc(ed.notify_telegram_token)+'"></div>',
    '<div class="frow"><span class="k">Chat ID</span><input type="text" id="txtTgChat" placeholder="私聊填数字 id；群/频道填 -100…" value="'+esc(ed.notify_telegram_chat_id)+'"></div>',
    ])+
    FGn('推送哪些事件',[
    '<div class="frow"><span class="k">今日配额达成</span><button class="sw'+(ed.notify_quota_done?' on':'')+'" id="swQuotaNotify" title="开=当天学习/打工打满你设的配额时推送（含当前截图）"></button></div>',
    '<div class="frow"><span class="k">隐藏职业解锁</span><button class="sw'+(ed.notify_career?' on':'')+'" id="swCareerNotify" title="开=武术家/梦境旅人/大明星解锁时推送（含职业树截图）"></button></div>',
    '<div class="frow"><span class="k">完成类通知总开关</span><button class="sw'+(ed.notify_event_notify?' on':'')+'" id="swEventNotify" title="关掉后所有「完成」类通知（如配额达成）都不发；任务失败告警不受影响"></button></div>',
    ])+
    FGn('异常提醒',[
    '<div class="frow"><span class="k">异常降级提醒</span><button class="sw'+(ed.notify_error_notify?' on':'')+'" id="swErrNotify" title="开=出现「没崩但静默降级」的错误时推送，如同类错误 30 分钟内最多一条。典型：配置读取失败后一直沿用旧配置，界面改什么都不生效"></button></div>',
    '<div class="frow" style="display:block"><span style="color:var(--sub);font-size:12px;line-height:1.6">'
    +'任务失败告警（学习/打工反复失败后退出调度器）<b>始终会发</b>，不受本页开关影响；'
    +'这里的开关只控制「完成通知」与「异常降级提醒」。'
    +'</span></div>',
    ])+
    FGn('测试与说明',[
    '<div class="frow"><span class="k">测试</span><span class="ctrl">'
    +'<span id="notifyTestMsg"></span>'
    +'<button class="minibtn" id="btnTestNotify">发送测试通知</button></span></div>',
    '<div class="noterow"><span class="nt">飞书</span><span class="nb">'
    +'群 → 右上角设置 → 群机器人 → 添加机器人 → 自定义机器人，复制 webhook 地址；'
    +'安全设置选「签名校验」就把密钥填到加签密钥（选「自定义关键词」可留空，关键词需含"QQ宠物"）。'
    +'</span></div>',
    '<div class="noterow"><span class="nt">Telegram</span><span class="nb">'
    +'跟 @BotFather 发 /newbot 建机器人拿 Token；<b>先给机器人发一条消息</b>，'
    +'再用 @userinfobot 查自己的 Chat ID（群/频道是 -100 开头的负数）。'
    +'</span></div>',
    '<div class="noterow"><span class="nt">告警</span><span class="nb">'
    +'任务失败告警始终会发（不受上面开关影响）；职业解锁与配额达成各有一个开关。'
    +'</span></div>',
    ]);
  ['#swFeishu','#swTg','#swQuotaNotify','#swCareerNotify','#swEventNotify','#swErrNotify'].forEach(id=>{
    const el=$(id); if(el) el.onclick=()=>{ el.classList.toggle('on'); };
  });
  const tn=$('#btnTestNotify');
  if(tn) tn.onclick=async()=>{
    const msg=$('#notifyTestMsg');
    msg.style.color='var(--sub)'; msg.textContent='先保存当前设置…';
    try{
      await saveNotifySettings(true);   // silent：不在保存区提示，只在测试行显示
      msg.textContent='正在发送…';
      const r=await fetch('/api/notify/test',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({target:'all'})});
      const d=await r.json();
      msg.style.color=d.ok?'#16a34a':'#b45309';
      msg.textContent=(d.ok?'✅ ':'✗ ')+(d.msg||'');
    }catch(e){
      msg.style.color='#b45309'; msg.textContent='测试失败：'+e.message;
    }
  };
}

async function saveNotifySettings(silent){
  if(!setInit) return;
  const msg=$('#notifySaveMsg');   // 保存按钮已移除（改动即自动保存）
  const updates={};
  const sw=(id,key)=>{const el=$(id); if(!el)return; const v=el.classList.contains('on');
                      if(!!v!==!!setInit[key]) updates[key]=v;};
  const tx=(id,key)=>{const el=$(id); if(!el)return; const v=el.value.trim();
                      if(v!==(setInit[key]||'')) updates[key]=v;};
  sw('#swFeishu','notify_feishu_enabled');   tx('#txtFsHook','notify_feishu_webhook');
  tx('#txtFsSecret','notify_feishu_secret');
  sw('#swTg','notify_telegram_enabled');     tx('#txtTgToken','notify_telegram_token');
  tx('#txtTgChat','notify_telegram_chat_id');
  sw('#swQuotaNotify','notify_quota_done');  sw('#swCareerNotify','notify_career');
  sw('#swEventNotify','notify_event_notify'); sw('#swErrNotify','notify_error_notify');
  if(!Object.keys(updates).length){
    if(!silent&&msg){ msg.className='saveMsg'; msg.textContent='没有改动'; }
    return {ok:true, changed:0};
  }
  try{
    const r=await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})});
    const d=await r.json();
    if(d.rejected&&d.rejected.length){
      if(!silent&&msg){ msg.className='saveMsg err'; msg.textContent='部分未保存：'+d.rejected.join('；'); }
      return {ok:false, rejected:d.rejected};
    }
    // 保存成功：把快照同步成新值，避免下次又把它当"改动"重复提交
    Object.keys(updates).forEach(k=>setInit[k]=updates[k]);
    if(!silent&&msg){ msg.className='saveMsg'; msg.textContent='✅ 已保存，下一轮调度生效'; }
    refreshData();
    return {ok:true, changed:Object.keys(updates).length};
  }catch(e){
    if(!silent&&msg){ msg.className='saveMsg err'; msg.textContent='保存失败：'+e.message; }
    return {ok:false, err:String(e.message||e)};
  }finally{
    }
}
// ---- 内页自动分节：把散装内容按"小标题 + 白卡"归拢（照设置页排版） ----
// 各内页结构不一，这里用 DOM 包装统一：
//   .subh / .advcap 作为小节标题 -> 其后到下一个标题之前的内容包进一张白卡。
(function(){
  // 注意：notify/set 自带 .fgrp+.fsec 分节结构，不能再套 .pgsec（会双层白卡）
  // 注意：log 页已手写 .pgsec 分节（工具栏/输出/异常截图），不能再自动包
  const PAGES=['adv','plan'];
  function wrap(page){
    const sec=document.querySelector('main > [data-page="'+page+'"]');
    if(!sec || sec.dataset.wrapped==='1') return;
    const kids=[...sec.children].filter(el=>!el.classList.contains('navhead'));
    if(!kids.length) return;
    const groups=[]; let cur=null;
    kids.forEach(el=>{
      const isTitle = el.classList.contains('subh') || el.classList.contains('advcap');
      if(isTitle){ cur={title:el.textContent.trim(), els:[]}; groups.push(cur); el.remove(); }
      else if(cur){ cur.els.push(el); }
      else { cur={title:'', els:[el]}; groups.push(cur); }
    });
    // 清掉空组
    const use=groups.filter(g=>g.els.length);
    if(!use.length) return;
    const frag=document.createDocumentFragment();
    use.forEach(g=>{
      const secEl=document.createElement('div'); secEl.className='pgsec';
      if(g.title){ const t=document.createElement('div'); t.className='pgsec-t'; t.textContent=g.title; secEl.appendChild(t); }
      const c=document.createElement('div'); c.className='pgsec-c';
      g.els.forEach(e=>c.appendChild(e));
      secEl.appendChild(c);
      frag.appendChild(secEl);
    });
    // 插到 navhead 之后
    const nh=sec.querySelector('.navhead');
    if(nh) nh.after(frag); else sec.insertBefore(frag, sec.firstChild);
    sec.dataset.wrapped='1';
  }
  function run(){ PAGES.forEach(wrap); }
  run();
  window.__wrapPages=run;   // 数据刷新后重建元素时再跑
})();

// ---- 改动即自动保存（已移除所有"保存设置"按钮） ----
// 标记脏 + 600ms 防抖后自动提交（连续输入不会每次都发请求）；
// setDirty 同时用于阻止定时刷新重建表单覆盖用户输入。
let _autoT=null;
function markDirtyAndSave(){
  setDirty=true;
  _lastFormTouch=Date.now();
  if(_autoT) clearTimeout(_autoT);
  _autoT=setTimeout(async()=>{
    _autoT=null;
    try{
      if(document.querySelector('#setForm')  && $('#setForm').offsetParent!==null)  await saveSettings();
      if(document.querySelector('#notifyForm')&& $('#notifyForm').offsetParent!==null) await saveNotifySettings(true);
    }catch(e){ /* 保存失败已在各自函数内提示 */ }
  },600);
}
// ---- ADB 卡片：列设备 / 连接无线设备 ----
async function refreshAdb(){
  const box=$('#adbDevices'); if(!box) return;
  box.textContent='查询中…';
  try{
    const d=await j('/api/adb');
    const devs=d.devices||[];
    const head='当前 adb：'+esc(d.resolved||'（未找到）')
      +'<br>配置的序列号：'+(d.serial?esc(d.serial):'（空 = 用第一台在线设备）');
    if(!devs.length){
      box.innerHTML=head
        +(d.error?'<br><span style="color:var(--warn)">'+esc(d.error)+'</span>':'')
        +'<br>没有在线设备。真机请插 USB 并在手机上允许调试；模拟器 / 无线调试在下面填地址点「连接」。';
      return;
    }
    box.innerHTML=head+devs.map(x=>{
      const on=x.state==='device', cur=(d.serial===x.serial);
      return '<br><span style="color:'+(on?'var(--ok)':'var(--warn)')+'">●</span> '
        +'<b>'+esc(x.serial)+'</b>'+(x.model?'（'+esc(x.model)+'）':'')
        +' <span style="color:var(--sub)">'+esc(x.state)+'</span>'
        +(cur?' <span style="color:var(--accent)">← 当前配置</span>':'')
        +' <button class="minibtn" data-adbserial="'+esc(x.serial)+'">用这台</button>';
    }).join('');
    box.querySelectorAll('[data-adbserial]').forEach(b=>{
      b.onclick=()=>{
        const el=$('#txtAdbSerial'); if(!el) return;
        el.value=b.dataset.adbserial; markDirtyAndSave();
        const m=$('#adbMsg');
        if(m){ m.style.color='var(--ok)'; m.textContent='已填入序列号 —— 重启调度器后生效'; }
      };
    });
  }catch(e){
    box.innerHTML='<span style="color:var(--warn)">查询失败：'+esc(e.message)+'</span>';
  }
}
async function adbConnectNow(){
  const msg=$('#adbMsg'), el=$('#txtAdbAddr');
  const addr=el?el.value.trim():'';
  if(!addr){ if(msg){ msg.style.color='var(--warn)'; msg.textContent='请先填连接地址'; } return; }
  if(msg){ msg.style.color='var(--sub)'; msg.textContent='连接中…'; }
  try{
    const r=await fetch('/api/adb/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({addr})});
    const d=await r.json();
    if(msg){ msg.style.color=d.ok?'var(--ok)':'var(--warn)';
             msg.textContent=(d.ok?'✅ ':'⚠ ')+(d.msg||''); }
    refreshAdb();
  }catch(e){
    if(msg){ msg.style.color='var(--warn)'; msg.textContent='连接失败：'+e.message; }
  }
}
document.addEventListener('input',e=>{
  if(!e.target||!e.target.closest) return;
  if(e.target.closest('#notifyForm')||e.target.closest('#setForm')) markDirtyAndSave();
});
document.addEventListener('change',e=>{
  if(!e.target||!e.target.closest) return;
  if(e.target.closest('#notifyForm')||e.target.closest('#setForm')) markDirtyAndSave();
});

async function saveSettings(){
  if(!setInit) return;
  const msg=$('#saveMsg');
  const updates={};
  const schoolEnabledNew = $('#swSchool').classList.contains('on');
  if(!!schoolEnabledNew !== !!setInit.school_enabled) updates.school_enabled=schoolEnabledNew;
  const fcEnabledNew = $('#swFC').classList.contains('on');
  if(!!fcEnabledNew !== !!setInit.friend_care_enabled) updates.friend_care_enabled=fcEnabledNew;
  const empEnabledNew = $('#swEmp').classList.contains('on');
  if(!!empEnabledNew !== !!setInit.employed_enabled) updates.employed_enabled=empEnabledNew;
  const gbEnabledNew = $('#swGiftBag').classList.contains('on');
  if(!!gbEnabledNew !== !!setInit.gift_bag_enabled) updates.gift_bag_enabled=gbEnabledNew;
  const hfEnabledNew = $('#swHF').classList.contains('on');
  if(!!hfEnabledNew !== !!setInit.hire_friend_enabled) updates.hire_friend_enabled=hfEnabledNew;
  const cwNew = $('#swCareer').classList.contains('on');
  if(!!cwNew !== !!setInit.career_watch) updates.career_watch=cwNew;
  const csNew = $('#swCareerStop').classList.contains('on');
  if(!!csNew !== !!setInit.career_stop_study) updates.career_stop_study=csNew;
  const hfNew = $('#swPkHf').classList.contains('on');
  if(!!hfNew !== !!setInit.pk_helper_fallback) updates.pk_helper_fallback=hfNew;
  const hwNew = $('#swHireWait').classList.contains('on');
  if(!!hwNew !== !!setInit.hire_wait) updates.hire_wait=hwNew;
  const getv=id=>($(id)?$(id).value.trim():'');
  const num=(id,key)=>{const v=getv(id); if(v==='')return; const n=parseInt(v,10); if(!isNaN(n)&&n!==setInit[key]) updates[key]=n;};
  const selc=(id,key)=>{const v=getv(id); if(v&&v!==setInit[key]) updates[key]=v;};
  const txtc=(id,key)=>{const v=getv(id); if(v!==(setInit[key]||'')) updates[key]=v;};
  selc('#selLoc','work_location'); selc('#selDur','work_duration'); selc('#selCare','care_method'); selc('#selFCMethod','friend_care_method'); selc('#selEmpAction','employed_action'); selc('#selMainOrder','main_order'); selc('#selSchoolAttr','school_attribute'); selc('#selSchoolDur','school_duration');
  txtc('#txtHire','hire_name');
  num('#numCoin','coin_threshold'); num('#numSchoolTimes','school_times');
  num('#numStudyQuota','study_quota_hours'); num('#numWorkQuota','work_quota_hours');
  // 合计停止点：一个输入框 → 后端写三个键（daily_hour_limit / work_stop_hours /
  // efficiency_tier2_hours，见 apply_settings 的 stop_total_keys）
  num('#numStopTotal','stop_total_hours');
  // 收益档（efficiency_tier1/2_hours）不是设置项：只在卡片里当说明文字显示，
  // 不走表单提交（config.yaml 里仍可手改，引擎照读）
  num('#numVisit','visit_times'); num('#numPk','pk_times'); num('#numAdv','adventure_times');
  selc('#selAdvType','adventure_type');
  txtc('#txtPkOnly','pk_only'); txtc('#txtPkSkip','pk_skip'); num('#numPkLv','pk_max_level'); txtc('#txtPkHelper','pk_helper');
  num('#numEnergy','care_energy'); num('#numClean','care_clean'); num('#numExchange','care_exchange'); num('#numGbInt','gift_bag_interval'); num('#numCareerInt','career_interval');
  txtc('#txtFCName','friend_care_name'); num('#numFCInt','friend_care_interval'); num('#numEmpInt','employed_interval');
  txtc('#txtFCRange','friend_care_range'); txtc('#txtGbRange','gift_bag_range');
  num('#numCareInt','care_interval'); num('#numHFTimes','hire_friend_times');
  // 连接层（ADB）：改完要重启调度器才生效，卡片里已提示
  txtc('#txtAdbPath','adb_path'); txtc('#txtAdbSerial','adb_serial');
  // 注意：通知渠道字段在**通知页**（#notifyForm），由 saveNotifySettings 单独提交，
  // 这里不要再取（那些 id 已不在本表单里，取了会是 null 而报错）。
  if(!Object.keys(updates).length){ msg.className='saveMsg'; msg.textContent='没有改动'; btn.disabled=false; return; }
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})});
    const d=await r.json();
    if(d.rejected&&d.rejected.length){ msg.className='saveMsg err'; msg.textContent='部分未保存：'+d.rejected.join('；'); }
    else { setDirty=false; msg.className='saveMsg'; msg.textContent='✅ 已保存，下一轮调度生效'; refreshData(); }
  }catch(e){ msg.className='saveMsg err'; msg.textContent='保存失败：'+e.message; }
  if(btn) btn.disabled=false;
}
// 保存按钮已移除 -> 改为改动即自动保存（见下）

$('#btnAltPreset').onclick=async()=>{
  const name=($('#txtAltMain')?$('#txtAltMain').value:'').trim();
  const msg=$('#presetMsg');
  if(!name){ msg.className='saveMsg err'; msg.textContent='先填大号名称（主人昵称或宠物名都能匹配）'; return; }
  const btn=$('#btnAltPreset'); btn.disabled=true;
  msg.className='saveMsg'; msg.textContent='应用中…';
  try{
    const r=await fetch('/api/preset/alt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    const d=await r.json();
    if(d.ok){
      setDirty=false;
      msg.className='saveMsg';
      msg.textContent='✅ 小号工具人模式已应用（'+Object.keys(d.applied||{}).length+' 项），下一轮调度生效';
      refreshData();
    } else {
      msg.className='saveMsg err';
      msg.textContent='未完全应用：'+((d.rejected||[]).join('；')||'未知');
    }
  }catch(e){ msg.className='saveMsg err'; msg.textContent='应用失败：'+e.message; }
  btn.disabled=false;
};

// 「画面」按钮：进实时画面页并直接开播（静态截图区已移除，不再轮询 /api/screenshot）
$('#btnShot').onclick=()=>{ showTab('shot'); if(!liveOn) liveStart(); };

// ---- 实时直播（设备端 scrcpy-server + 本机 ffmpeg 转 HLS；按需启停，服务端 30s 空闲自动回收）----
// iOS Safari 支持原生 HLS，直接喂 m3u8；桌面 Chrome 不支持原生 HLS，改走 MSE 拉同一份 fMP4 分片。
let liveOn=false, liveMs=null, liveSeen=new Set(), liveInfo={}, liveTimer=null, takeover=false, liveRestarting=false, liveLastSeek=0;
let liveSb=null, liveDbg={codec:'',init:0,segs:0,err:'',step:''};
function liveDbgLine(){
  const v=$('#liveVideo'); let buf=0;
  try{ buf=(liveSb&&liveSb.buffered.length)?(liveSb.buffered.end(liveSb.buffered.length-1)-liveSb.buffered.start(0)):0; }catch(e){}
  return 'step='+(liveDbg.step||'-')+' codec='+(liveDbg.codec||'?')+' init='+liveDbg.init+'B segs='+liveDbg.segs
    +' buf='+buf.toFixed(1)+'s t='+(v?Number(v.currentTime).toFixed(1):'-')
    +' rs='+(v?v.readyState:'-')+' paused='+(v?v.paused:'-')+(liveDbg.err?(' ERR='+liveDbg.err):'');
}
function liveHint(t){const el=$('#liveHint'); if(el){el.textContent=t||''; el.style.display=t?'block':'none';}}
// 用实际设备/视频尺寸校正占位比例（默认 9:20；一加 1080x2412 这类只差千分之几，校正后与画面完全对齐）
function applyLiveAspect(st){
  const dim=((st&&(st.video||st.device))||'').split('x').map(Number);
  const card=document.querySelector('.shotpage .livecard');
  if(card&&dim.length===2&&dim[0]>0&&dim[1]>0){
    card.style.setProperty('--live-ar-w',dim[0]);
    card.style.setProperty('--live-ar-h',dim[1]);
  }
}
async function liveStatus(){try{return await (await fetch('/api/stream/status',{cache:'no-store'})).json();}catch(e){return {running:false,error:String(e)};}}
function liveStop(){
  liveOn=false;
  if(liveTimer){clearInterval(liveTimer);liveTimer=null;}
  const v=$('#liveVideo');
  try{v.pause();v.removeAttribute('src');v.load();}catch(e){}
  liveMs=null; liveSeen=new Set();
  $('#btnLive').textContent='开始直播'; $('#btnLive').classList.remove('on');
  $('#liveMeta').textContent='';
  liveHint('已停止。服务端无人观看 30 秒后自动回收设备端编码。');
}
async function liveMse(v){
  // codec 串必须与设备实际编码 profile 一致，否则 addSourceBuffer 静默不解码（黑屏）。
  // 优先用服务端从 init.mp4 的 avcC 解析出的精确值，再用常见 profile 兜底试。
  const cands=[];
  if(liveInfo && liveInfo.codec)cands.push('video/mp4; codecs="'+liveInfo.codec+'"');
  ['avc1.64001f','avc1.42c01f','avc1.42e01f','avc1.4d401f','avc1.640028','avc1.640032','avc1.64001e']
    .forEach(c=>cands.push('video/mp4; codecs="'+c+'"'));
  const mime=cands.find(c=>window.MediaSource.isTypeSupported(c));
  if(!mime){ liveToast('浏览器不支持该视频编码，请改用 Safari 或手机端观看'); throw new Error('no supported codec'); }
  liveDbg.codec=mime.replace(/^video\/mp4; codecs="|"$/g,''); liveDbg.init=0; liveDbg.segs=0; liveDbg.err='';
  liveDbg.step='mksource';
  liveMs=new MediaSource(); liveSeen=new Set();
  v.src=URL.createObjectURL(liveMs);
  await new Promise(r=>liveMs.addEventListener('sourceopen',r,{once:true}));
  liveDbg.step='addsb';
  const sb=liveMs.addSourceBuffer(mime);
  liveSb=sb;
  liveDbg.step='fetchinit';
  const initBuf=await (await fetch('/stream/init.mp4?t='+Date.now(),{cache:'no-store'})).arrayBuffer();
  liveDbg.step='appendinit';
  try{ await new Promise(r=>{sb.addEventListener('updateend',r,{once:true}); sb.appendBuffer(initBuf);}); liveDbg.init=initBuf.byteLength; }
  catch(e){ liveDbg.err='init:'+e.name; }
  // 顺序很重要：先 pump 灌数据、播放用"点火不等待"。反过来会死锁 ——
  // play() 要等 MSE 有数据才 resolve，而数据要等 pump 去拉，两边互等（真实浏览器上表现为永远黑屏）。
  liveDbg.step='pump';
  const pump=async()=>{
    if(!liveOn)return;
    try{
      const txt=await (await fetch('/stream/index.m3u8?t='+Date.now(),{cache:'no-store'})).text();
      const segs=txt.split('\n').map(s=>s.trim()).filter(s=>s&&s.charAt(0)!=='#');
      // 全部未见分片都要 append：HLS 分片按时间切（非关键帧起始），只补最新两片会缺少
      // 参考帧、MSE 直接解不出来（表现为黑屏）；延迟交给追帧与缓冲裁剪控制，不靠丢分片。
      for(const s of segs){
        if(!liveOn)return;
        if(liveSeen.has(s))continue;
        liveSeen.add(s);
        const buf=await (await fetch('/stream/'+s,{cache:'no-store'})).arrayBuffer();
        try{ await new Promise(r=>{sb.addEventListener('updateend',r,{once:true}); sb.appendBuffer(buf);});
             if(liveDbg.segs===0)v.play().catch(()=>{});   // 有数据了再点火
             liveDbg.segs++; }
        catch(e){ liveDbg.err='seg:'+e.name; liveSeen.delete(s); }   // 失败允许下轮重试
      }
      // 缓冲只留最近约 3 秒：MSE 缓冲无上限增长会撞 QuotaExceeded，之后新分片全都追加不进
      if(sb.buffered.length){
        const st=sb.buffered.start(0), en=sb.buffered.end(sb.buffered.length-1);
        // 只在缓冲明显过长时裁掉播放点之前的部分（保留 v.currentTime-1 起的内容）
        if(en-st>10){ const cut=Math.max(st, v.currentTime-1); if(cut>st) try{ sb.remove(st,cut); }catch(e){} }
      }
    }catch(e){}
    setTimeout(pump,300);
  };
  pump();
  v.play().catch(()=>{});
}
async function liveStart(){
  $('#btnLive').textContent='连接中…';
  if(liveTimer){clearInterval(liveTimer);liveTimer=null;}   // 重连时别叠加定时器
  const st=await liveStatus();
  liveInfo=st||{};
  applyLiveAspect(liveInfo);
  const v=$('#liveVideo');
  if(!st.running && st.error){liveHint('启动失败：'+st.error); $('#btnLive').textContent='重试'; return;}
  liveHint('缓冲中…（设备端采集启动约 3~7 秒）');
  liveOn=true;
  // 播放路径选择（踩过坑）：**不能**用 canPlayType 的真值判断 ——
  // Chrome 对 'application/vnd.apple.mpegurl' 返回 "maybe"（真值！），会误走原生 HLS 分支，
  // 而 Chrome 并不支持 HLS → m3u8 加载不出画面（黑屏）。只有 Safari/WebKit 返回 "probably"。
  // 规则：iPhone（无 MSE）走原生 HLS；其余（桌面 Chrome/Edge/Safari）一律走 MSE，延迟也更可控。
  const isIOS=/iPhone|iPad|iPod/.test(navigator.userAgent);
  const nativeHls=v.canPlayType('application/vnd.apple.mpegurl')==='probably';
  try{
    if(isIOS || (!window.MediaSource && nativeHls)){
      v.src='/stream/index.m3u8?t='+Date.now();   // iOS Safari 原生 HLS
      v.play().catch(()=>{});
    }else if(window.MediaSource){
      await liveMse(v);                           // 桌面：MSE + fMP4 分片
    }else{
      liveOn=false; liveHint('当前浏览器既不支持 MSE 也不支持原生 HLS，请用 Chrome/Safari'); return;
    }
  }catch(e){
    // 绝不静默：把失败原因显示出来（踩过"黑屏但什么都不说"的坑）
    liveDbg.err='start:'+((e&&e.name)||'')+' '+((e&&e.message)||'');
    liveOn=false; $('#btnLive').textContent='重试';
    liveHint('直播启动失败：'+liveDbg.err); return;
  }
  $('#btnLive').textContent='停止直播'; $('#btnLive').classList.add('on');
  liveHint('');
  liveTimer=setInterval(async()=>{
    if(!liveOn)return;
    try{ await fetch('/api/stream/ping'+(window.__autoLive?('?dbg='+encodeURIComponent(liveDbgLine())):''),{method:'POST'}); }catch(e){}   // 心跳保活；自动开播时回传诊断
    const s=await liveStatus();
    liveInfo=s;                       // 保持尺寸最新（点击换算用）
    // 延迟看门狗：HLS（尤其 iOS 原生播放器）会自行缓冲、延迟越滚越大，
    // 落后超过阈值就跳到直播边缘 —— 否则会从 2~3 秒一路涨到 8~9 秒。
    const lag=liveLag();
    $('#liveMeta').textContent=(lag!==null?('延迟 '+lag.toFixed(1)+'s · '):'')
      +(s.running?('流运行中 '+(s.uptime||0)+'s'):'流已停止');
    if(!s.running && s.error)liveHint('流异常：'+s.error);
    if(window.__autoLive)liveHint(liveDbgLine());   // 自动开播模式下把 MSE 状态显示出来，便于截图定位
    // 追帧：阈值 1.2s（原来 2s —— 那等于把稳态延迟锁在 2 秒以上）；3 秒内最多追一次，
    // 避免频繁 seek 让 iOS 原生播放器反复重新缓冲。
    if(lag!==null && lag>1.0 && Date.now()-liveLastSeek>3000){
      liveLastSeek=Date.now();
      const v=$('#liveVideo');
      try{ v.currentTime=Math.max(0,v.seekable.end(v.seekable.length-1)-0.2); }catch(e){}
      liveToast('已追到直播边缘（原落后 '+lag.toFixed(1)+'s）',1600);
    }
    // 接管状态跟随调度器：别处（仪表盘/GUI）又把调度器拉起来时，注入会被服务端拒绝，这里同步失效
    if(takeover && s.scheduler_alive && !s.scheduler_paused){
      takeover=false;
      const b=$('#btnTakeover'); if(b){b.classList.remove('on'); b.textContent='接管操作';}
      liveToast('调度器已恢复运行，接管失效（需要再点一次接管）',2800);
    }
  },3000);
}
$('#btnLive').onclick=()=>{ liveOn?liveStop():liveStart(); };
// 操作反馈气泡：注入成功/失败/前置条件不满足都要说话，否则"点了没反应"无从排查
let liveToastTimer=null;
function liveToast(msg,ms){
  const el=$('#liveToast'); if(!el)return;
  el.textContent=msg; el.classList.add('on');
  if(liveToastTimer)clearTimeout(liveToastTimer);
  liveToastTimer=setTimeout(()=>el.classList.remove('on'),ms||1800);
}
// 取「视频尺寸 + 设备尺寸」：首次开播时 status 早于流启动、video 可能为空，这里按需补查一次
async function liveDims(){
  const pick=s=>{
    const ok=a=>a.length===2&&a[0]>0&&a[1]>0;
    const v=((s&&s.video)||'').split('x').map(Number);
    const d=((s&&s.device)||'').split('x').map(Number);
    if(ok(v)&&ok(d))return {video:v,device:d};
    if(ok(v))return {video:v,device:v};        // 设备尺寸缺失时按同一比例换算（scrcpy 保持宽高比）
    return null;
  };
  let r=pick(liveInfo);
  if(!r){ liveInfo=await liveStatus(); r=pick(liveInfo); }
  return r;
}
// 当前延迟（秒）= 可 seek 末端 - 播放位置；MSE 下 seekable 等于 buffered，iOS 原生 HLS 下是 m3u8 窗口
function liveLag(){
  const v=$('#liveVideo'); if(!v)return null;
  try{
    const sk=v.seekable;
    if(!sk||!sk.length)return null;
    const lag=sk.end(sk.length-1)-v.currentTime;
    return (isFinite(lag)&&lag>0)?lag:0;
  }catch(e){ return null; }
}
$('#btnTakeover').onclick=async()=>{
  const btn=$('#btnTakeover');
  if(takeover){
    takeover=false; btn.classList.remove('on'); btn.textContent='接管操作';
    try{ await fetch('/api/runner/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }catch(e){}
    liveToast('已交还控制权，调度器继续跑',2600); return;
  }
  try{
    const st=await liveStatus();
    if(!st.scheduler_alive){          // 调度器本来没跑，直接接管
      takeover=true; btn.classList.add('on'); btn.textContent='已接管·点击生效';
      liveToast('已接管（调度器未运行）',2600); return;
    }
    const r=await (await fetch('/api/runner/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    if(!r||!r.ok){ btn.textContent='接管失败'; liveToast('接管失败：'+((r&&r.msg)||'未知原因')); return; }
    // 让路在"当前步骤完成后"生效：轮询等它真正停下（最多 60 秒），期间不打断任务
    btn.textContent='等待让路…'; liveHint('调度器正在完成当前步骤，随后让路…（不会中断任务、不丢进度）');
    for(let i=0;i<40;i++){
      await new Promise(res=>setTimeout(res,1500));
      const s2=await liveStatus();
      if(!s2.scheduler_alive || s2.scheduler_paused){
        takeover=true; btn.classList.add('on'); btn.textContent='已接管·点击生效';
        liveHint(''); liveToast('已接管：调度器已让路（任务未中断）',3000); return;
      }
    }
    btn.textContent='接管失败';
    liveToast('调度器 60 秒内未让路；可回总览页点「停止」强制结束',3800);
  }catch(e){ btn.textContent='接管失败'; liveToast('接管请求失败：'+e.message); }
};
// 点击画面 → 注入。绑在透明覆盖层上（iOS 的 <video> 会吞掉 click），且每个分支都给反馈
$('#liveTap').addEventListener('pointerdown',async ev=>{
  if(!liveOn){ liveToast('请先点「开始直播」'); return; }
  if(!takeover){ liveToast('请先点「接管操作」（会停掉调度器）'); return; }
  const dims=await liveDims();
  if(!dims){ liveToast('还没拿到画面尺寸，等 1~2 秒再点'); return; }
  const v=$('#liveVideo'), rect=v.getBoundingClientRect();
  const vw=dims.video[0], vh=dims.video[1], dw=dims.device[0], dh=dims.device[1];
  const sc=Math.min(rect.width/vw, rect.height/vh)||1;
  const vidX=(ev.clientX-rect.left-(rect.width-vw*sc)/2)/sc;
  const vidY=(ev.clientY-rect.top-(rect.height-vh*sc)/2)/sc;
  const x=Math.round(vidX*dw/vw), y=Math.round(vidY*dh/vh);
  try{
    const r=await (await fetch('/api/stream/tap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({x:x,y:y})})).json();
    if(r&&r.ok){ liveToast('已点击 ('+x+', '+y+')'); return; }
    const msg=(r&&r.msg)||'未知原因';
    liveToast('注入被拒：'+msg,2600);
    // 流被空闲回收/断开时自动重连，避免"操作一会儿突然就不能动"
    if(msg.indexOf('流未运行')>=0 && !liveRestarting){
      liveRestarting=true; liveHint('流已断开，正在自动重连…');
      try{ await liveStart(); }catch(e){}
      liveRestarting=false;
    }
  }catch(e){ liveToast('请求失败：'+e.message); }
});
// 切走该页 / 页面隐藏即停止拉流，让服务端尽快回收（挂机期间不占设备编码器）
document.querySelectorAll('#tabbar button, #tabbar2 button, [data-back]').forEach(b=>{
  b.addEventListener('click',()=>{ if(liveOn && b.dataset.tab!=='shot') liveStop(); });
});
document.addEventListener('visibilitychange',()=>{ if(document.hidden&&liveOn&&!window.__autoLive) liveStop(); });


// 任务开关：点击勾选 → 写入 config（ruamel 保注释），调度器下一轮热加载生效
document.getElementById('taskList').addEventListener('click', async ev => {
  const cb = ev.target.closest('.mcb'); if(!cb) return;
  const k = cb.dataset.k; if(!k) return;
  const on = !cb.classList.contains('on');
  cb.classList.toggle('on', on);
  const nm = cb.parentElement.querySelector('.mname');
  if(nm) nm.classList.toggle('off', !on);
  try {
    await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({updates:{[k+'_enabled']:on}})});
    setTimeout(refreshData, 1500);
  } catch(e) { cb.classList.toggle('on'); setTimeout(refreshData, 800); }
});

// 状态栏配色跟随当前页（iOS 独立 Web App 下状态栏那条带取页面背景色，
// 见 <meta name="theme-color"> 上方注释）。
// 色值**只定义在 CSS**（--qp-statusbar，按场景/深浅色自动取值），这里读出来同步给
// meta —— 不在 JS 里再抄一份色表，否则以后改色必然漏一处。
// **注意**：必须在 showTab 的 `if(name===curTab) return;` 之前调用 ——
// 否则"首帧就是内页"（localStorage 记住的 tab / ?tab=set 直开）时不会被同步。
function syncThemeColor(name){
  const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  let want = '#FFFFFF';   // 内页顶栏是白的
  if(name==='main'){
    const v = getComputedStyle(document.documentElement)
                .getPropertyValue('--qp-statusbar').trim();
    if(v) want = v;
    else want = dark ? '#A9722D' : '#D5A758';   // CSS 变量读不到时的兜底
  }
  document.querySelectorAll('meta[name="theme-color"]').forEach(function(m){
    // 两组 meta 各带一个 media 查询，只改属于当前主题的那条，
    // 免得把另一主题的值也覆盖成当前主题的色（切系统主题时就会串色）
    const isDark = (m.getAttribute('media')||'').indexOf('dark') >= 0;
    if(isDark === !!dark) m.setAttribute('content', want);
  });
}
if(window.matchMedia){
  // 切系统深浅色：房间背景图（room-*-dark）与状态栏采样色都要跟着换。
  // 手动固定的家居背景没有夜间版，applyManualScene 会重设成同一张 —— 幂等，不用特判。
  try{ window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', ()=>{
    applyManualScene();
    syncThemeColor(curTab);
  }); }catch(e){}
}

// 当前页（供 history 手势判断）
let curTab = 'main';
// 导航层级：**0 = 总览，1 = 内页，2 = 设置二级**。
//
// **历史模型（第十二轮修）：栈与"导航层级"严格对应，最多三层**
//   往下一层        pushState      （总览 -> 内页、设置一级 -> 二级）
//   同级互切        replaceState   （内页 <-> 内页，栈不增长）
//   往上一层        history.back() （**退栈**，不是再压一条上层页）
//   跨层回总览      一次退够（go(-navDepth)）
//
// 改前是"访问轨迹"模型：每次切页都 push，连"返回"按钮也 push，于是
//   ① 侧滑要一路退回**访问过的每一页**（实测 总览→日志→设置→设置二级→冒险→职业
//      之后要滑 5 次才回总览，用户反馈"右滑了很多很多次才回到总览页"）
//   ② 点"返回"回总览后再侧滑，**又回到刚才那个内页**（栈里刚压了一条总览）
// 根因就一句话：**按钮在压栈、手势在退栈，两者方向相反**。
// 现在层级与栈一一对应：二级侧滑 -> 一级，再侧滑 -> 总览（用户要的两段式钻取）。
let navDepth = 0;
let pendingTab = null;   // 跳级退栈（二级 -> 别的内页）时，退到总览后再压目标页
function showTab(name, skipHistory){
  // 离开设置页时重置层级，下次进入从一级列表开始
  if(name!=='set' && window.__setGrp) window.__setGrp=null;
  // 同理：离开日志页时重置子页（下次进来停在"实时日志"）
  if(name!=='log' && window.__logSub) window.__logSub=null;
  document.querySelectorAll('main > [data-page]').forEach(el=>el.classList.toggle('hide', el.dataset.page!==name));
  // 日志页子页：默认子页是"实时日志"，只有显式进过收益记录（或 popstate 指明）才是它
  if(name==='log' && !window.__logSub) showLogIndex(true);
  // 内页（非总览）把 html 底色切白：body 的 padding-top 露出的就是 html 底色，
  // 总览露房间暖色图、内页露白色顶栏（见 CSS 里 html[data-page] 那条注释）
  document.documentElement.setAttribute('data-page', name);
  syncThemeColor(name);   // 状态栏同步（须在下面的早退之前，见函数注释）
  // 两列导航都要更新选中态（#tabbar2 是右列，早期漏了）
  // #btnScene 例外：它占着 data-tab="main" 但不是 tab（是"切换房间背景"），
  // 刷 .on 会在总览页给它常驻白环、撑大一圈（历史上那个"返回键大一圈"就是这么来的）。
  document.querySelectorAll('#tabbar button, #tabbar2 button').forEach(
    b=>b.classList.toggle('on', b.id!=='btnScene' && b.dataset.tab===name));
  try{localStorage.setItem('qpet_tab',name);}catch(e){}
  // **层级同步必须放在早退之前**：侧滑回到"设置一级"时页面名没变（set -> set），
  // 若放在早退之后，navDepth 会停在 2，接着点"返回总览"就会多退一层（甚至退出应用）。
  if(skipHistory) navDepth = (name==='main') ? 0 : 1;
  if(name===curTab) return;
  curTab=name;
  // 侧滑/浏览器返回（popstate）驱动的切换：层级已同步，不再动历史
  if(skipHistory) return;
  // 跳级退栈（go(-2)）还在路上：本次只渲染页面、不动历史，
  // 否则连续两次切页会把栈算歪，最坏情况退过头直接退出应用。
  if(pendingTab){ navDepth = (name==='main') ? 0 : 1; return; }
  // showTab 只处理页面级（总览 0 / 内页 1）；设置二级由 openSetGroup 负责（层级 2）
  const target = (name==='main') ? 0 : 1;
  const url = (name==='main') ? (location.pathname + rootSearch) : ('?tab='+name);
  try{
    if(target > navDepth){                       // 往下一层：压栈
      navDepth = target;
      history.pushState({tab:name}, '', url);
    } else if(target === navDepth){              // 同级互切：替换，栈不增长
      if(target === 1) history.replaceState({tab:name}, '', url);
    } else if(target === 0){                     // 回总览：一次退到根
      const steps = navDepth;
      navDepth = 0;
      if(steps === 1) history.back(); else history.go(-steps);
    } else {                                     // 二级 -> 别的内页：先退到总览，落定后再压目标页
      pendingTab = name;
      navDepth = 0;
      history.go(-2);
    }
  }catch(e){}
}
// 侧滑/浏览器返回：退到栈里上一条。已在总览（根条目）则放行，让浏览器正常退栈。
window.addEventListener('popstate', function(e){
  const st=e.state||{};
  if(pendingTab){   // 跳级退栈的收尾：落回总览后再压目标页（见 showTab 最后一条分支）
    const t=pendingTab; pendingTab=null;
    try{ history.pushState({tab:t}, '', '?tab='+t); }catch(err){}
    navDepth=1;
    showTab(t, true);
    return;
  }
  const t=st.tab || 'main';
  // 设置页：按这条记录的 grp 决定停在一级还是二级（层级也要跟着落定：
  // showTab 只认页面级 0/1，二级的 2 必须在这里补上，否则"返回设置列表"会判不出该退栈）
  if(t==='set'){
    showTab('set', true);
    if(st.grp){ openSetGroup(st.grp, true); navDepth=2; }
    else { showSetIndex(true); navDepth=1; }
    return;
  }
  // 日志页子页：同设置页二级，层级 2 要在这里补上（showTab 只认页面级 0/1）
  if(t==='log'){
    showTab('log', true);
    if(st.sub==='reward'){ openLogReward(true); navDepth=2; }
    else { showLogIndex(true); navDepth=1; }
    return;
  }
  if(t===curTab) return;   // 栈里这条就是当前页（"回总览"的 back() 落到总览就是这种）
  showTab(t, true);
});
// tab 按钮：#tabbar（左列4个）+ #tabbar2（右列3个）都要绑
// #btnScene 例外：它虽在 #tabbar 里且带 data-tab="main"（保住 §INTEGRATION 1 的
// `#tabbar button` 契约），但功能是换房间背景 —— 绑 showTab('main') 只会早退（什么都不做）。
// 它自己的绑定（点按开选择面板 / 长按切下一张）在 setupSceneBtn 里。
document.querySelectorAll('#tabbar button, #tabbar2 button').forEach(b=>{
  if(b.id==='btnScene') return;
  b.onclick=()=>showTab(b.dataset.tab);
});
// 内页返回（.home 里的导航会随总览页一起隐藏，故内页需要独立返回入口）
// 内页顶栏返回按钮（统一 .backbtn[data-back]；二级设置页的 #btnSetBack 有自己的处理）
document.addEventListener('click',function(e){
  const b=e.target.closest && e.target.closest('.backbtn[data-back]');
  if(b){ showTab(b.dataset.back||'main'); }
},true);
let initTab='main';
try{initTab=localStorage.getItem('qpet_tab')||'main';}catch(e){}
// 支持 ?tab=set 直开某页（便于分享链接/截图/调试）
try{
  const qp=new URLSearchParams(location.search).get('tab');
  // 注意：#tabbar 只含左列 4 个按钮，set/log/shot 在 #tabbar2 —— 要全局找
  if(qp && document.querySelector('button[data-tab="'+qp+'"]')) initTab=qp;
}catch(e){}
// ?tab=set&grp=<key> 直开设置页的某个二级分组（同理，便于分享链接/截图/调试）。
// **必须在这里读、并且存进独立变量**，两个坑：
//   ① showTab('main') 会清 window.__setGrp（"离开设置页重置层级"），存那里会被抹掉；
//   ② showTab('set') 会把 URL 重写成 '?tab=set'，之后再读 location.search 就没有 grp 了。
let _liveFromUrl=false;
try{ _liveFromUrl=new URLSearchParams(location.search).get('live')==='1'; }catch(e){}
let _grpFromUrl='';
try{ _grpFromUrl=new URLSearchParams(location.search).get('grp')||''; }catch(e){}
// ?tab=log&sub=reward 直开收益记录子页，同样必须**在这里**读走（showTab('log')
// 会把 URL 重写成 '?tab=log'，之后 location.search 里就没有 sub 了）
let _logSubFromUrl='';
try{ _logSubFromUrl=new URLSearchParams(location.search).get('sub')||''; }catch(e){}
// 初始：先取好友名单（下拉选择用），再渲染 tab —— 否则首次渲染
// 的 datalist 是空的，用户以为"没有好友可选"
(async function initFriends(){
  try{
    const r=await fetch('/api/friends'); const d=await r.json();
    if(Array.isArray(d.friends) && d.friends.length){
      FRIENDS=d.friends;
      // 名单到手后重渲染一次设置表单，让下拉项立刻可用
      if(window.__lastEditable) renderSettings(window.__lastEditable);
    }
  }catch(e){}
})();
// 初始历史：**根条目永远是总览** —— 这样"内页 -> 总览"用 history.back() 一定落回
// 总览，而不会退到浏览器的上一页/直接退出应用（曾把 initTab 直接 replace 成根条目，
// 那样根条目可能是内页，退栈就退到应用外了）。
// 直开内页（?tab=set / localStorage 记住的页）时再压一层，栈 = [总览, 内页]。
// 查询串只清 `?tab=`（当前页由 history.state 决定），**其它参数要保留**
// ——曾整串丢掉，连 ?case= 这种无关参数一起没了。
let rootSearch='';
try{
  // tab 与 sub 都是"当前停在哪一页"的参数，不属于根条目（其余参数要保留）
  const sp=new URLSearchParams(location.search); sp.delete('tab'); sp.delete('sub');
  const qs=sp.toString(); rootSearch = qs ? ('?'+qs) : '';
}catch(e){}
try{ history.replaceState({tab:'main'}, '', location.pathname + rootSearch); }catch(e){}
curTab='main';
showTab('main', true);
if(initTab!=='main') showTab(initTab);
// ?tab=log&sub=reward 直开收益记录子页（与设置页 ?tab=set&grp= 同款，便于分享/截图）
try{
  if(initTab==='log' && _logSubFromUrl==='reward') openLogReward();
}catch(e){}

// 房间背景：首帧就应用（不能等第一次 /api/data —— 手动选过的背景会先闪一下主房间再跳；
// 「自动」也有必要走一遍：data-scene 与状态栏色要落定）。
// 放在这里而不是场景那块：setSceneAttr 里读了 curTab（上面刚初始化完，
// 提前调用会撞 let 的暂时性死区）。
applyManualScene();

setInterval(()=>{if(!document.hidden)refreshLogs()},3000);
setInterval(()=>{if(!document.hidden)refreshData()},6000);
setInterval(()=>{if(!document.hidden)refreshAdventure()},10000);
// 收益记录只在子页打开时才拉（按次记录一天就十几条，10s 足够）
setInterval(()=>{if(!document.hidden && window.__logSub==='reward')refreshRewards()},10000);
setInterval(()=>{if(!document.hidden)refreshPlan()},15000);
refreshData();refreshLogs();refreshAdventure();refreshPlan();
// ?tab=shot&live=1 直接进实时画面并开播（分享链接/调试/自动验收用）
// 注意：不能用 location.search 现读 —— showTab 会重写 URL 把 live 参数抹掉（同 grp 的坑）
if(_liveFromUrl){ window.__autoLive=true; showTab('shot'); liveStart(); }
// 占位框一开始就用设备真实比例（只读 status，不会启动流）
liveStatus().then(applyLiveAspect).catch(()=>{});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshData();refreshLogs();refreshAdventure();refreshPlan()}});
try{
  const okSW=('serviceWorker' in navigator)&&(location.protocol==='https:'||location.hostname==='localhost'||location.hostname==='127.0.0.1');
  if(okSW) navigator.serviceWorker.register('/sw.js').catch(()=>{});
}catch(e){}

// ---- 舞台单位基准：1dp = 可用宽度 / 480 ----
// 不用 CSS 的 100vw：它含滚动条宽度（实测 clientWidth 489 vs 100vw 500），
// 会让所有绝对坐标偏大约 2%。这里按真实可用宽度精确计算并写入 --vu。
(function(){
  function setU(){
    // 用视口宽度算 1dp（不再依赖 .home 存在 —— 设置页等页面没有 .home，
    // 早期版本把 --u 定义在 .home 上，导致那些页面所有 calc(var(--u)*N) 失效）
    var w=document.documentElement.clientWidth || window.innerWidth;
    document.documentElement.style.setProperty('--vu',(w/360)+'px');
  }
  setU();
  window.addEventListener('resize',setU);
  window.addEventListener('orientationchange',setU);
})();

// ---- 功能栏与任务列表底部对齐 ----
// 列表高度随任务数变化（9 项时约 227dp），固定 top 不是压住 deck 就是留大空。
// 渲染后按列表实际底部反推功能栏 top（功能栏总高 230dp：20 + 70*2 + 70 + 20 间距）。
(function(){
  var home=document.querySelector('.home'),
      tl=document.getElementById('taskList'),
      fb=document.querySelector('.funcbar');
  if(!home||!tl||!fb) return;
  var lastTop=null;
  function measure(){
    if(!tl.children.length) return null;           // 还没内容：别量（会量到"标题下面一点"）
    var u=parseFloat(getComputedStyle(home).getPropertyValue('--vu'))||(home.clientWidth/360);
    var hr=home.getBoundingClientRect(), tr=tl.getBoundingClientRect();
    var want=(tr.bottom-hr.top)/u;                 // 期望：功能栏底端 = 列表底端
    var h2=fb.getBoundingClientRect().height/u;    // 功能栏实际高
    var top=want-h2;
    if(top<100) top=319.3;                         // 列表过短时回官方位置
    return top;
  }
  function place(){
    // 列表还没内容时别摆（否则会量到"标题下面一点点"、命中兜底位置）；
    // 正常情况下 renderTaskSkeleton() 已经先铺好占位行，这里量到的就是最终高度。
    if(!tl.children.length){ fb.style.visibility='hidden'; return; }
    fb.style.visibility='';
    var top=measure(); if(top===null) return;
    // **迟滞**：与上次落点差 < 0.4u 就不动 —— 6 秒一次的数据刷新会让列表底部
    // 有亚像素级变化（字体/行高取整），每次都跟着改 top 就是用户看到的"偶尔上下抽动"。
    if(lastTop!==null && Math.abs(top-lastTop)<0.4) return;
    lastTop=top;
    fb.style.top='calc(var(--u) * '+top.toFixed(1)+')';
  }
  // **等布局稳定再量**：MutationObserver 在行重建的那一帧就会回调，此时量到的可能是
  // 中间态（行已清空/只插入一半）→ 位置先跳一下再跳回来。改成下一帧 + 再下一帧各量一次，
  // 取后一次（rAF 保证在样式/布局算完之后）。
  // 立即量一次（保证任何环境都会摆位；rAF 在无头/虚拟时间/后台标签下可能不触发），
  // 再在下一帧补量一次纠正"量在布局中间"的情况，最后加一个超时兜底。
  // 三次都走同一个 place()，靠上面的 0.4u 迟滞保证不会来回抖。
  var pending=false;
  function placeSoon(){
    place();
    if(pending) return;
    pending=true;
    var done=function(){ if(!pending) return; pending=false; place(); };
    if(window.requestAnimationFrame){
      requestAnimationFrame(function(){ requestAnimationFrame(done); });
    }
    setTimeout(done, 150);
  }
  renderTaskSkeleton();   // 先占住列表高度，place() 一次到位（见函数注释）
  place();
  window.addEventListener('resize',placeSoon);
  window.addEventListener('orientationchange',placeSoon);
  window.addEventListener('pageshow',placeSoon);      // 手机切回前台/从缓存恢复时重量
  var tlEl=document.getElementById('taskList');
  if(tlEl&&window.MutationObserver) new MutationObserver(placeSoon).observe(tlEl,{childList:true});
})();
