// 全局可变状态（M3/T3.3 从 app.js 抽出）。
//
// 这些是跨页面共享的可变状态：最近一次拉取的虚机/节点数据、分页、各节点硬件明细的展开态、
// 自动刷新定时器与渲染缓存。
//
// 为什么仍然是顶层 let 而不是 window.xxx：本项目用 classic script（非 ES module）多文件加载，
// 顶层 let/const 处于同一个全局词法环境，各文件共享 —— 其它文件里的赋值与读取照旧生效，
// 无需改写。内联 onclick 也依赖全局函数名（见 index.html），所以拆分刻意不动作用域模型。

let allGuests = [];       // 最近一次拉取的全部虚机
let allNodes = [];        // 集群节点列表，供工具弹窗选择执行节点
let selectedNode = null;  // 概览中选中的节点，用于过滤虚机列表
let expandedDisks = new Set();  // 磁盘列表已展开的节点，跨自动刷新保持
let expandedMem = new Set();    // 内存条明细已展开的节点，跨自动刷新保持
let expandedCpu = new Set();    // CPU 插槽明细已展开的节点，跨自动刷新保持
let expandedNics = new Set();   // 网卡列表已展开的节点，跨自动刷新保持
let hwPref = new Map();         // 节点卡片硬件明细展开状态（node → bool）；未记录的按默认策略
let guestPage = 1;              // 虚机列表当前页码，跨自动刷新保持
let guestPageByNode = {};       // 各节点（含「全部」）各自记住的页码，切节点互不干扰
let guestPageSize = 10;         // 虚机列表每页条数（10/20/30 可切）
let refreshTimer = null;  // 自动刷新定时器
let lastGuestHtml = null;  // 上一次虚机列表渲染结果，用于「局部更新」跳过无变化时的整段重绘；null = 缓存失效（DOM 里不是这份结果，如骨架屏）
