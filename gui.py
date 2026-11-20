# -*- coding: utf-8 -*-
"""指数新高新低复盘 - 桌面版（传统Windows风格 · 一级数据源下拉 + 二级子菜单）
布局：顶部工具栏（数据源[指数/板块/大盘]/立即刷新/导出xlsx） + 主体三区
  - 指数区：三个横向子选项卡（上证50/沪深300/中证2000），各自 每日统计 + 个股明细
  - 板块区：三列每日统计表（日期 | OBV强势板块数 | OBV强势且60日线上方数）
  - 大盘区：12列每日情绪统计表（红盘/绿盘/涨停/跌停/炸板/连板/最高板，问财口径含ST）
底部状态栏；启动后台预加载各数据源到内存，切换零计算直接渲染
数据：复用 server.py 计算核心（成分股csv/板块指数OBV/问财大盘统计 + SQLite缓存）
性能：大盘表 Canvas 直绘（免千级 Label 控件）、重绘 60ms 防抖、缓存新鲜免联网、
      SQLite WAL 多线程并行读、init_db 仅启动执行一次
"""
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, font as tkfont, messagebox
from datetime import datetime
from zoneinfo import ZoneInfo

import server  # 复用计算核心

TZ = ZoneInfo('Asia/Shanghai')

# 日志文件：frozen 时放 exe 同目录，源码运行放同目录
if getattr(sys, 'frozen', False):
    _LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), '复盘_gui.log')
else:
    _LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '复盘_gui.log')


def _log(msg):
    try:
        with open(_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")} {msg}\n')
    except Exception:
        pass


def _err_text(e, limit=120):
    """异常转单行可读文本（界面提示用，过长截断避免撑爆状态栏/画布）。"""
    s = f'{type(e).__name__}: {e}'.replace('\n', ' ').strip()
    return s if len(s) <= limit else s[:limit] + '…'


# UI 偏好配置（窗口大小/位置、上次数据源等）：与日志同目录，损坏时静默回退默认
_CFG_FILE = os.path.join(os.path.dirname(_LOG_FILE), '复盘_ui.json')


def _load_ui_config():
    try:
        with open(_CFG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _save_ui_config(cfg):
    try:
        with open(_CFG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# 单实例互斥体句柄：进程存活期间必须保持引用，否则句柄被回收、互斥体失效。
_MUTEX_HANDLE = None


def _focus_existing_window():
    """把已运行实例的主窗口恢复并激活到前台（重复启动时给出明确反馈）。"""
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        found = []
        cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith('指数新高新低复盘'):
                found.append(hwnd)
            return True

        u32.EnumWindows(cb_type(_cb), 0)
        if found:
            u32.ShowWindow(found[0], 9)          # SW_RESTORE：从最小化恢复
            u32.SetForegroundWindow(found[0])
        return bool(found)
    except Exception:
        return False


def _single_instance_guard():
    """单实例守卫：返回 True 表示本进程可以继续启动，False 表示已有实例在运行。

    原实现允许无限多开，实测同时开 3 个实例时，其中一个实例的三个预加载线程会
    全部静默失败（界面永久空白、连启动日志都写不进去），用户只能看到白屏。
    这里用命名互斥体（按 exe 所在目录区分，不同副本互不影响）拦截重复启动，
    并把已有窗口激活到前台，给出明确反馈。
    """
    global _MUTEX_HANDLE
    if not sys.platform.startswith('win'):
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        key = hashlib.md5(
            os.path.normcase(os.path.dirname(_LOG_FILE)).encode('utf-8')).hexdigest()[:12]
        _MUTEX_HANDLE = k32.CreateMutexW(None, False, f'zshxwxl_fupan_{key}')
        if not _MUTEX_HANDLE:
            return True                      # 创建失败不阻塞启动
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            k32.CloseHandle(_MUTEX_HANDLE)
            _MUTEX_HANDLE = None
            if not _focus_existing_window():
                ctypes.windll.user32.MessageBoxW(
                    None,
                    '程序已经在运行了（窗口可能被其他窗口挡住或已最小化）。\n'
                    '请在任务栏中查看「指数新高新低复盘」窗口。',
                    '指数新高新低复盘', 0x40)
            return False
        return True
    except Exception:
        return True                          # 守卫自身异常不影响正常启动


# 传统Windows灰蓝配色
COLOR_BG = '#eef1f5'
COLOR_PANEL = '#f7f9fb'
COLOR_TOOLBAR = '#dde3ea'
COLOR_HEADER = '#4a5a6a'
COLOR_RED = '#d64541'
COLOR_RED_DARK = '#b03a37'
COLOR_TEXT = '#1c1f26'
COLOR_HIGH = '#c0392b'
COLOR_LOW = '#1e8449'

INDEX_NAMES = {'000016': '上证50', '000300': '沪深300', '932000': '中证2000'}
INDEX_CODES = list(INDEX_NAMES.keys())
SRC_INDEX, SRC_BLOCK, SRC_MARKET, SRC_MARGIN = '指数', '板块', '大盘', '两融'
CHART_DAYS = 30  # 折线图仅显示最近 N 个交易日
PRELOAD_LABELS = ('上证50', '沪深300', '中证2000', '板块', '大盘', '两融')


def _fmt_amount(v, signed=False):
    """金额格式化（亿元，千分位）；None 显示 '-'，signed=True 带正负号"""
    if v is None:
        return '-'
    return f'{v:+,.2f}' if signed else f'{v:,.2f}'

# 大盘情绪统计表：列key/表头/列宽/前景色
MARKET_COLS = [
    ('date', '日期', 92, COLOR_TEXT),
    ('vol', '成交总额', 88, COLOR_TEXT),
    ('red', '红盘', 70, COLOR_RED),
    ('green', '绿盘', 70, COLOR_LOW),
    ('zt', '实际涨停', 82, COLOR_RED),
    ('fzt', '非一字涨停', 88, COLOR_RED),
    ('dt', '跌停', 64, COLOR_LOW),
    ('zhaban', '炸板', 64, '#c87f0a'),
    ('lb', '连板', 64, COLOR_RED),
    ('lb2', '二连板', 72, COLOR_RED),
    ('lb3', '三连板', 72, COLOR_RED),
    ('lb3p', '三连板以上', 84, COLOR_RED),
    ('high_lb', '最高板(非ST)', 92, COLOR_HIGH),
]
MARKET_HEAD_H, MARKET_ROW_H = 30, 25  # 大盘 Canvas 表格：表头高/行高（渲染、行点击、悬停共用）


class App:
    def __init__(self, root):
        self.root = root
        root.title(f'指数新高新低复盘  {server.APP_VERSION}')
        root.geometry('1080x720')
        root.minsize(900, 600)
        # 恢复上次关闭时的窗口大小/位置（配置损坏或非法时回退默认值）。
        # 坐标做过屏幕外过滤：Windows 下窗口最小化时 geometry 返回
        # "-32000" 类坐标，原样恢复会让窗口"看不见也点不到"
        self.ui_cfg = _load_ui_config()
        geo = self.ui_cfg.get('geometry')
        _m = re.match(r'^(\d+x\d+)([+-]\d+)([+-]\d+)$', geo) if isinstance(geo, str) else None
        if _m and int(_m.group(2)) > -3000 and int(_m.group(3)) > -3000:
            root.geometry(geo)
        root.configure(bg=COLOR_BG)
        # 关闭窗口时置位 _stop：让自动更新 watchdog 与预加载线程优雅退出。
        # 原实现从不置位 _stop，watchdog 线程会一直空转到进程退出。
        root.protocol('WM_DELETE_WINDOW', self._on_close)

        self.cur_src = SRC_MARKET     # 当前一级数据源：大盘 / 指数 / 板块 / 两融（默认大盘）
        self.cur_index = '000016'     # 当前指数（指数区子选项卡）
        self.results = {}             # {指数代码: result} 预加载缓存
        self.block_result = None      # 板块 OBV 逐日统计结果
        self.market_result = None     # 大盘每日情绪统计结果
        self.loading = False          # 指数加载/刷新中
        self.block_loading = False    # 板块统计加载中
        self.market_loading = False   # 大盘统计加载中
        self.margin_result = None     # 两融融资余额统计结果
        self.margin_loading = False   # 两融统计加载中
        self._preloading = set()      # 预加载中的指数代码集合（三指数并行，故不能用单个 bool）
        self._stop = False
        self._chart_job = None        # 指数折线图重绘防抖任务
        self._market_job = None       # 大盘视图重绘防抖任务
        self._margin_job = None       # 两融视图重绘防抖任务
        self._market_key = None       # 大盘 Canvas 重绘缓存键（数据+尺寸未变则跳过）
        self._prog_ts = 0.0           # 大盘回填进度节流时间戳（两融补齐共用）
        self._vol_backfill_ts = 0.0   # 成交总额后台回填防抖时间戳（避免数据源故障时反复空转）
        self._auto_updating = False   # 每日自动更新是否正在进行（watchdog 据此不重复开线程）
        # 各数据源加载失败原因：原实现失败只写日志，界面停在"正在加载…"的不可能状态，
        # 用户无法区分"还在加载"和"已经失败"，这里记录下来供界面显式提示。
        self.market_err = None        # 大盘统计加载失败原因
        self.block_err = None         # 板块统计加载失败原因
        self.margin_err = None        # 两融统计加载失败原因
        self.idx_err = {}             # {指数名称: 加载失败原因}
        self.db_err = None            # SQLite 初始化失败原因
        self._sort_state = {}         # {tree: {col: 是否降序}} 表头排序状态
        self._sort_orig = {}          # {tree: {col: 原表头文字}} 排序箭头还原用
        self._market_hover_row = -1   # 大盘表格悬停行（-1=无）
        self._market_width = 0        # 大盘表格总宽度（悬停高亮矩形宽度用）
        self._chart_meta = {}         # {canvas: 折线图坐标变换与序列值}（悬停取值用）
        self._tip_font = None         # 折线图悬停数值面板字体（root 创建后初始化）
        self.chart_days = CHART_DAYS  # 折线图显示窗口（30/60/90/全部，工具栏可切换）
        self._block_job = None        # 板块折线图窗口缩放重绘的防抖任务
        self._preload_pending = set(PRELOAD_LABELS)  # 启动预加载未完成的数据源

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        try:
            self._tip_font = tkfont.Font(root=root, family='Microsoft YaHei UI', size=9)
        except Exception:
            self._tip_font = None
        self._refresh_preload_status()   # 启动聚合进度：正在加载数据源（剩 6 项）
        # 全局快捷键：F5=立即刷新、Ctrl+E=导出xlsx、Ctrl+1~4=切换数据源
        root.bind('<F5>', lambda e: self._on_refresh())
        root.bind('<Control-e>', lambda e: self._on_export())
        root.bind('<Control-E>', lambda e: self._on_export())
        for _i, _src in enumerate((SRC_MARKET, SRC_INDEX, SRC_BLOCK, SRC_MARGIN), 1):
            root.bind(f'<Control-Key-{_i}>', lambda e, s=_src: self._select_src(s))

        # DB 初始化只做一次（WAL 等）；原三个预加载线程各做一次属重复开销
        try:
            server.init_db()
        except Exception as e:
            self.db_err = _err_text(e)
            _log(f'init_db ERR {e}\n{traceback.format_exc()}')

        # ---- UI 线程安全队列 ----
        # 后台线程禁止直接调 root.after（mainloop 启动前调用会抛
        # "main thread is not in main loop"导致预加载失败、缓存永不更新、
        # 每次启动都全量重拉）。改为：后台线程把回调放入队列，
        # 主线程进入 mainloop 后经 _poll_ui 轮询串行执行，彻底消除该竞态。
        self._ui_q = queue.Queue()
        # 主线程注册轮询（__init__ 在主线程，注册安全；mainloop 启动后触发）
        self.root.after(30, self._poll_ui)

        # 启动预加载：三指数并行后台计算（各自独立线程，先算好的先可用）；
        # 同时并行预加载板块 OBV 逐日统计与大盘情绪统计
        # （缓存新鲜则免联网秒出，不新鲜自动联网更新到最新交易日）
        # 预先置位加载标志：线程真正跑起来前存在毫秒级空窗，避免恢复上次
        # 数据源时误判"未在加载"而重复开线程（server 侧虽有全局锁兜底，
        # 但多一路请求就多一次等待）
        self.block_loading = self.market_loading = self.margin_loading = True
        for idx in INDEX_CODES:
            threading.Thread(target=self._preload_one,
                             args=(idx,), daemon=True).start()
        threading.Thread(target=self._preload_block, daemon=True).start()
        threading.Thread(target=self._preload_market, daemon=True).start()
        threading.Thread(target=self._preload_margin, daemon=True).start()

        # 每日自动更新：交易日收盘后自动把三大数据源刷新到最新交易日
        # （程序常驻跨天运行时，避免数据停留在旧交易日需要手动点"立即刷新"）
        self._auto_date = None      # 已自动更新到的日期（YYYY-MM-DD）
        self._auto_refreshing = False
        threading.Thread(target=self._auto_update_watchdog, daemon=True).start()
        # 两融专项看门狗：当日数据当晚才发布/接口偶发失败时静默补齐
        threading.Thread(target=self._margin_watchdog, daemon=True).start()

        # 恢复上次的数据源与指数子选项卡（首次启动用默认：大盘/上证50）。
        # 预加载线程启动与置位 loading 标志之间存在毫秒级窗口，为避免恢复时
        # 误判"未在加载"而重复开线程，预加载线程启动前标志已预先置位。
        src = self.ui_cfg.get('source')
        if src in (SRC_MARKET, SRC_INDEX, SRC_BLOCK, SRC_MARGIN) and src != SRC_MARKET:
            self.src_combo.set(src)
            self._on_nav_change()
        tab = self.ui_cfg.get('index_tab')
        if self.cur_src == SRC_INDEX and isinstance(tab, int) \
                and 0 <= tab < len(INDEX_CODES):
            self.idx_notebook.select(tab)
        self._update_refresh_btn_state()

    # ---------- UI 线程安全投递 ----------
    def _ui_call(self, fn):
        """后台线程 -> 主线程回调投递：入队，由主线程 _poll_ui 轮询执行。
        替代直接 self.root.after(0,...)（跨线程调用在 mainloop 前会抛 RuntimeError）。"""
        try:
            self._ui_q.put(fn)
        except Exception:
            pass

    def _poll_ui(self):
        """主线程轮询 UI 队列，串行执行后台线程投递的回调。"""
        while True:
            try:
                fn = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                # 记录完整调用栈：widget 被销毁后仍被回调访问的 TclError 需定位到具体回调
                _log(f'ui_cb ERR {type(e).__name__}: {e}\n{traceback.format_exc()}')
        self.root.after(30, self._poll_ui)

    # ---------- 启动预加载进度聚合 ----------
    def _preload_end(self, label):
        """某数据源预加载结束（成功或失败都算）：刷新聚合进度提示"""
        self._preload_pending.discard(label)
        self._refresh_preload_status()

    def _refresh_preload_status(self):
        """启动阶段状态栏聚合显示剩余待加载数据源。
        原 6 路预加载线程各自写状态栏、互相覆盖一闪一闪，用户看不出
        还差哪些数据没就绪；聚合后一目了然，全部结束即不再覆盖视图状态。"""
        pending = [lb for lb in PRELOAD_LABELS if lb in self._preload_pending]
        if not pending:
            return
        self._set_status(f'正在加载数据源（剩 {len(pending)} 项）：'
                         + '　'.join(pending))

    # ---------- UI 构建 ----------
    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=COLOR_TOOLBAR, height=52)
        bar.pack(side='top', fill='x')
        bar.pack_propagate(False)

        tk.Label(bar, text='数据源：', bg=COLOR_TOOLBAR, fg=COLOR_TEXT,
                 font=('Microsoft YaHei UI', 10)).pack(side='left', padx=(12, 2), pady=10)
        self.src_combo = ttk.Combobox(bar, state='readonly', width=8,
                                      values=[SRC_MARKET, SRC_INDEX, SRC_BLOCK, SRC_MARGIN],
                                      font=('Microsoft YaHei UI', 10))
        self.src_combo.current(0)
        self.src_combo.bind('<<ComboboxSelected>>', lambda e: self._on_nav_change())
        self.src_combo.pack(side='left', padx=2, pady=10)

        self.refresh_btn = tk.Button(bar, text='立即刷新', command=self._on_refresh,
                                     bg=COLOR_RED, fg='white', activebackground=COLOR_RED_DARK,
                                     activeforeground='white', relief='flat', bd=0,
                                     font=('Microsoft YaHei UI', 10, 'bold'), padx=18, pady=4)
        self.refresh_btn.pack(side='left', padx=(30, 6), pady=10)

        self.export_btn = tk.Button(bar, text='导出xlsx', command=self._on_export,
                                    bg=COLOR_HEADER, fg='white', activebackground='#3a4856',
                                    activeforeground='white', relief='flat', bd=0,
                                    font=('Microsoft YaHei UI', 10), padx=14, pady=4)
        self.export_btn.pack(side='left', padx=6, pady=10)

        # 图表周期切换（右端）：作用于指数/板块/大盘/两融全部折线图
        self.chart_days_combo = ttk.Combobox(bar, state='readonly', width=6,
                                             values=['30日', '60日', '90日', '全部'],
                                             font=('Microsoft YaHei UI', 9))
        self.chart_days_combo.current(0)
        self.chart_days_combo.bind('<<ComboboxSelected>>',
                                   lambda e: self._on_chart_days_change())
        self.chart_days_combo.pack(side='right', padx=(2, 12), pady=10)
        tk.Label(bar, text='图表周期：', bg=COLOR_TOOLBAR, fg=COLOR_TEXT,
                 font=('Microsoft YaHei UI', 9)).pack(side='right', padx=(6, 2), pady=10)

    def _create_tab_view(self, parent):
        """创建一个数据视图：每日统计 Treeview + 个股明细 Treeview + 两张折线图"""
        main_frame = tk.Frame(parent, bg=COLOR_PANEL)
        main_frame.pack(fill='both', expand=True, pady=(6, 6))
        cols = ('date', 'high', 'low', 'basis')
        main_tree = ttk.Treeview(main_frame, columns=cols, show='headings', height=8)
        main_tree.heading('date', text='日期')
        main_tree.heading('high', text='创新高数')
        main_tree.heading('low', text='创新低数')
        main_tree.heading('basis', text='基差')
        main_tree.column('date', width=140, anchor='center')
        main_tree.column('high', width=100, anchor='center')
        main_tree.column('low', width=100, anchor='center')
        main_tree.column('basis', width=100, anchor='center')
        vsb = ttk.Scrollbar(main_frame, orient='vertical', command=main_tree.yview)
        main_tree.configure(yscrollcommand=vsb.set)
        main_tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        main_tree.bind('<<TreeviewSelect>>', lambda e: self._show_detail())

        det_frame = tk.Frame(parent, bg=COLOR_PANEL)
        det_frame.pack(fill='both', expand=True, pady=(0, 6))
        det_header = tk.Label(det_frame, text='个股明细（点击上方日期行查看当日新高/新低个股）',
                              bg=COLOR_PANEL, fg=COLOR_HEADER,
                              font=('Microsoft YaHei UI', 10, 'bold'))
        det_header.pack(anchor='w', pady=(0, 2))
        dcols = ('code', 'name', 'kind')
        detail_tree = ttk.Treeview(det_frame, columns=dcols, show='headings', height=5)
        detail_tree.heading('code', text='代码')
        detail_tree.heading('name', text='名称')
        detail_tree.heading('kind', text='类型')
        detail_tree.column('code', width=120, anchor='center')
        detail_tree.column('name', width=200, anchor='w')
        detail_tree.column('kind', width=100, anchor='center')
        dvsb = ttk.Scrollbar(det_frame, orient='vertical', command=detail_tree.yview)
        detail_tree.configure(yscrollcommand=dvsb.set)
        detail_tree.pack(side='left', fill='both', expand=True)
        dvsb.pack(side='right', fill='y')

        # 两张折线图：创新高/新低数量（红/绿） + 基差
        charts_frame = tk.Frame(parent, bg=COLOR_PANEL)
        charts_frame.pack(fill='both', expand=True, pady=(0, 6))
        chart1 = tk.Canvas(charts_frame, bg='white', height=190, highlightthickness=0)
        chart1.pack(side='left', fill='both', expand=True, padx=(0, 4))
        chart2 = tk.Canvas(charts_frame, bg='white', height=190, highlightthickness=0)
        chart2.pack(side='left', fill='both', expand=True)
        chart1.bind('<Configure>', lambda e: self._schedule_chart_redraw())
        chart2.bind('<Configure>', lambda e: self._schedule_chart_redraw())

        main_tree.tag_configure('odd', background='#f2f5f8')
        detail_tree.tag_configure('odd', background='#f2f5f8')
        self._attach_sort(main_tree)
        self._attach_sort(detail_tree)
        self._attach_copy_menu(main_tree)
        self._attach_copy_menu(detail_tree)

        return {'main': main_tree, 'detail': detail_tree, 'detail_header': det_header,
                'chart1': chart1, 'chart2': chart2}

    def _create_block_view(self, parent):
        """板块视图：三列每日统计表 + 下方两栏板块名称区（选中日期行联动）"""
        main_frame = tk.Frame(parent, bg=COLOR_PANEL)
        main_frame.pack(fill='both', expand=True, pady=(6, 6))
        cols = ('date', 'obv_strong', 'obv_above60')
        main_tree = ttk.Treeview(main_frame, columns=cols, show='headings')
        main_tree.heading('date', text='日期')
        main_tree.heading('obv_strong', text='OBV强势(剔除60日线上方)')
        main_tree.heading('obv_above60', text='被剔除(站上60日线)')
        main_tree.column('date', width=140, anchor='center')
        main_tree.column('obv_strong', width=200, anchor='center')
        main_tree.column('obv_above60', width=180, anchor='center')
        vsb = ttk.Scrollbar(main_frame, orient='vertical', command=main_tree.yview)
        main_tree.configure(yscrollcommand=vsb.set)
        main_tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        main_tree.bind('<<TreeviewSelect>>', lambda e: self._show_block_names())

        # 中部折线图：OBV 强势数 / 被剔除数趋势。
        # 板块页此前只有表格 + 名称区，没有趋势视角；固定 180px 高、宽随窗口自适应
        charts_frame = tk.Frame(parent, bg=COLOR_PANEL, height=180)
        charts_frame.pack(fill='x', pady=(0, 6))
        charts_frame.pack_propagate(False)
        chart_strong = tk.Canvas(charts_frame, bg='#fdfdfd', highlightthickness=0)
        chart_strong.pack(side='left', fill='both', expand=True, padx=(0, 4))
        chart_above60 = tk.Canvas(charts_frame, bg='#fdfdfd', highlightthickness=0)
        chart_above60.pack(side='left', fill='both', expand=True, padx=(4, 0))
        chart_strong.bind('<Configure>', lambda e: self._schedule_block_redraw())
        chart_above60.bind('<Configure>', lambda e: self._schedule_block_redraw())

        # 下方名称区：两栏并排（OBV强势板块 | OBV强势且60日线上方板块）
        names_frame = tk.Frame(parent, bg=COLOR_PANEL)
        names_frame.pack(fill='both', expand=True, pady=(0, 6))

        left = tk.Frame(names_frame, bg=COLOR_PANEL)
        left.pack(side='left', fill='both', expand=True, padx=(0, 6))
        tk.Label(left, text='OBV强势板块(剔除60日线上方)', bg=COLOR_PANEL, fg=COLOR_HEADER,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', pady=(0, 2))
        strong_tree = ttk.Treeview(left, columns=('name',), show='headings', height=6)
        strong_tree.heading('name', text='板块名称')
        strong_tree.column('name', width=200, anchor='w')
        svsb = ttk.Scrollbar(left, orient='vertical', command=strong_tree.yview)
        strong_tree.configure(yscrollcommand=svsb.set)
        strong_tree.pack(side='left', fill='both', expand=True)
        svsb.pack(side='right', fill='y')

        right = tk.Frame(names_frame, bg=COLOR_PANEL)
        right.pack(side='left', fill='both', expand=True)
        tk.Label(right, text='被剔除板块(站上60日线)', bg=COLOR_PANEL, fg=COLOR_HEADER,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', pady=(0, 2))
        above_tree = ttk.Treeview(right, columns=('name',), show='headings', height=6)
        above_tree.heading('name', text='板块名称')
        above_tree.column('name', width=200, anchor='w')
        avsb = ttk.Scrollbar(right, orient='vertical', command=above_tree.yview)
        above_tree.configure(yscrollcommand=avsb.set)
        above_tree.pack(side='left', fill='both', expand=True)
        avsb.pack(side='right', fill='y')

        for tv in (main_tree, strong_tree, above_tree):
            tv.tag_configure('odd', background='#f2f5f8')
            self._attach_sort(tv)
            self._attach_copy_menu(tv)

        return {'main': main_tree, 'strong': strong_tree, 'above': above_tree,
                'chart_strong': chart_strong, 'chart_above60': chart_above60}

    def _create_margin_view(self, parent):
        """两融视图：沪深京融资余额逐日表 + 两张折线图（合计走势 / 日增减）"""
        frame = tk.Frame(parent, bg=COLOR_PANEL)
        frame.pack(fill='both', expand=True, pady=(6, 6))
        cols = ('date', 'sh', 'sz', 'bj', 'total', 'chg')
        tree = ttk.Treeview(frame, columns=cols, show='headings')
        for c, head, width in (('date', '日期', 120),
                               ('sh', '沪市融资余额', 130),
                               ('sz', '深市融资余额', 130),
                               ('bj', '京市融资余额', 130),
                               ('total', '合计', 130),
                               ('chg', '较前一日', 110)):
            tree.heading(c, text=head)
            tree.column(c, width=width, anchor='center')
        vsb = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        # 日增减：增=红，减=绿（A股习惯）
        tree.tag_configure('up', foreground=COLOR_RED)
        tree.tag_configure('down', foreground=COLOR_LOW)
        tree.tag_configure('odd', background='#f2f5f8')
        self._attach_sort(tree)
        self._attach_copy_menu(tree)

        charts = tk.Frame(parent, bg=COLOR_PANEL)
        charts.pack(fill='both', expand=True, pady=(0, 6))
        c1 = tk.Canvas(charts, bg='white', height=190, highlightthickness=0)
        c1.pack(side='left', fill='both', expand=True, padx=(0, 4))
        c2 = tk.Canvas(charts, bg='white', height=190, highlightthickness=0)
        c2.pack(side='left', fill='both', expand=True)
        c1.bind('<Configure>', lambda e: self._schedule_margin_redraw())
        c2.bind('<Configure>', lambda e: self._schedule_margin_redraw())
        return {'main': tree, 'chart1': c1, 'chart2': c2}

    def _create_market_view(self, parent):
        """大盘视图：Canvas 直绘 12列每日情绪统计表（上方滚动）+ 最高板/连板双折线图（下方固定）。
        直接在 Canvas 上画矩形+文本（比每格一个 Label 快一个数量级，98行12列渲染<50ms）"""
        outer = tk.Frame(parent, bg=COLOR_PANEL)
        outer.pack(fill='both', expand=True, pady=(6, 6))
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        frame = tk.Frame(outer, bg=COLOR_PANEL)
        frame.grid(row=0, column=0, sticky='nsew')

        canvas = tk.Canvas(frame, bg='white', highlightthickness=0)
        vsb = ttk.Scrollbar(frame, orient='vertical', command=canvas.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=canvas.xview)
        canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side='right', fill='y')
        hsb.pack(side='bottom', fill='x')
        canvas.pack(side='left', fill='both', expand=True)

        # 鼠标滚轮：仅当光标位于大盘视图内时接管，避免影响其他视图
        def _on_wheel(e):
            canvas.yview_scroll(-1 * int(e.delta / 120), 'units')
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _on_wheel))

        def _on_leave(_e):
            canvas.unbind_all('<MouseWheel>')
            self._on_market_hover(None)

        canvas.bind('<Leave>', _on_leave)
        canvas.bind('<Motion>', self._on_market_hover)
        # 窗口尺寸变化：防抖后按新宽度重绘（列宽自适应拉伸铺满）
        canvas.bind('<Configure>', lambda e: self._schedule_market_redraw())
        # 点击数据行：弹出当日涨停/跌停/炸板/连板明细窗口
        canvas.bind('<Button-1>', lambda e: self._on_market_row_click(e))

        # 表格下方：两张折线图（最高板 + 连板），不随表格滚动，固定显示在底部
        charts = tk.Frame(outer, bg=COLOR_PANEL)
        charts.grid(row=1, column=0, sticky='ew', pady=(6, 0))
        chart_hlb = tk.Canvas(charts, bg='white', height=160, highlightthickness=0)
        chart_hlb.pack(side='left', fill='both', expand=True, padx=(0, 4))
        chart_lb = tk.Canvas(charts, bg='white', height=160, highlightthickness=0)
        chart_lb.pack(side='left', fill='both', expand=True)
        chart_hlb.bind('<Configure>', lambda e: self._schedule_market_redraw())
        chart_lb.bind('<Configure>', lambda e: self._schedule_market_redraw())

        return {'canvas': canvas, 'chart_hlb': chart_hlb, 'chart_lb': chart_lb}

    def _build_body(self):
        body = tk.Frame(self.root, bg=COLOR_BG)
        body.pack(side='top', fill='both', expand=True, padx=10, pady=(8, 4))

        # 指数区：三个横向子选项卡
        self.idx_frame = tk.Frame(body, bg=COLOR_BG)
        self.idx_notebook = ttk.Notebook(self.idx_frame)
        self.idx_notebook.pack(fill='both', expand=True)
        self.tab_views = []
        for name in INDEX_NAMES.values():
            tab = tk.Frame(self.idx_notebook, bg=COLOR_BG)
            self.idx_notebook.add(tab, text=name)
            self.tab_views.append(self._create_tab_view(tab))
        self.idx_notebook.bind('<<NotebookTabChanged>>', lambda e: self._on_idx_tab_changed())

        # 板块区：三列每日统计表
        self.block_frame = tk.Frame(body, bg=COLOR_BG)
        self.block_view = self._create_block_view(self.block_frame)

        # 大盘区：12列每日情绪统计表
        self.market_frame = tk.Frame(body, bg=COLOR_BG)
        self.market_view = self._create_market_view(self.market_frame)

        # 两融区：沪深京融资余额逐日表 + 走势图
        self.margin_frame = tk.Frame(body, bg=COLOR_BG)
        self.margin_view = self._create_margin_view(self.margin_frame)

        # 默认显示大盘区（一级下拉默认选中第一个=大盘）
        self.market_frame.pack(fill='both', expand=True)
        self.block_frame.pack_forget()
        self.margin_frame.pack_forget()
        self.idx_frame.pack_forget()

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value='正在加载数据…')
        bar = tk.Label(self.root, textvariable=self.status_var, anchor='w',
                       bg=COLOR_TOOLBAR, fg=COLOR_TEXT, font=('Microsoft YaHei UI', 9))
        bar.pack(side='bottom', fill='x')

    # ---------- 当前状态 ----------
    def _current_result(self):
        """当前数据源结果：板块源返回板块 OBV 逐日统计，大盘源返回大盘情绪统计，
        指数源返回当前子选项卡指数结果"""
        if self.cur_src == SRC_BLOCK:
            return self.block_result
        if self.cur_src == SRC_MARKET:
            return self.market_result
        if self.cur_src == SRC_MARGIN:
            return self.margin_result
        return self.results.get(self.cur_index)

    def _current_view(self):
        if self.cur_src == SRC_BLOCK:
            return self.block_view
        if self.cur_src == SRC_MARKET:
            return self.market_view
        if self.cur_src == SRC_MARGIN:
            return self.margin_view
        ti = self.idx_notebook.index(self.idx_notebook.select())
        return self.tab_views[ti]

    def _clear_view(self):
        """清空当前数据源对应视图（Treeview 清行；大盘视图清空 Canvas 绘制内容；
        折线图 Canvas 不在此清理，由 _redraw_charts 重绘时覆盖）"""
        view = self._current_view()
        if self.cur_src == SRC_MARKET:
            view['canvas'].delete('all')
            self._market_key = None  # 画布已清空，缓存键失效
            self._market_days = None  # 行命中数据同步失效（防清空后仍可点击旧行）
            return
        for w in view.values():
            if isinstance(w, ttk.Treeview):
                w.delete(*w.get_children())

    # ---------- 表格通用体验：隔行底色 / 表头排序 / 右键复制 ----------
    @staticmethod
    def _stripe(tree):
        """按当前行序重设隔行底色标签（保留行上已有的前景色等其它标签）"""
        for i, iid in enumerate(tree.get_children('')):
            tags = [t for t in tree.item(iid, 'tags') or [] if t != 'odd']
            if i % 2 == 1:
                tags.append('odd')
            tree.item(iid, tags=tags)

    def _attach_sort(self, tree):
        """表头点击排序：自动识别数值列（缺失值'-'排在末尾），再点一次反向"""
        for col in tree['columns']:
            tree.heading(col, command=lambda c=col, t=tree: self._sort_tree(t, c))

    def _sort_tree(self, tree, col):
        items = tree.get_children('')
        if not items:
            return
        # 以控件对象本身为键（而非 id()）：大盘明细弹窗的 Treeview 会频繁
        # 创建销毁，id 被新控件复用时，旧排序状态会污染新表格的表头文字还原
        orig = self._sort_orig.setdefault(tree, {})
        if col not in orig:
            orig[col] = tree.heading(col, 'text')
        state = self._sort_state.setdefault(tree, {})
        prev = state.get('last')
        if prev and prev != col:
            tree.heading(prev, text=orig.get(prev, prev))
        desc = not state.get(col, False)
        state[col] = desc
        state['last'] = col
        tree.heading(col, text=f'{orig[col]}{" ▼" if desc else " ▲"}')

        def _key(iid):
            s = str(tree.set(iid, col)).replace(',', '').replace('%', '').strip()
            if s in ('', '-'):
                return (0, 0)
            try:
                return (1, float(s))
            except ValueError:
                return (2, s)

        for idx, iid in enumerate(sorted(items, key=_key, reverse=desc)):
            tree.move(iid, '', idx)      # move 而非重建：保留选中状态与前景色标签
        self._stripe(tree)

    def _reset_sort(self, tree):
        """数据重渲染后清除排序箭头（行序已恢复为数据序，箭头不再成立）"""
        orig = self._sort_orig.get(tree) or {}
        state = self._sort_state.get(tree)
        if not state:
            return
        last = state.get('last')
        if last and last in orig:
            tree.heading(last, text=orig[last])
        state.pop('last', None)

    def _attach_copy_menu(self, tree):
        """右键菜单 / Ctrl+C 复制选中行（制表符分隔，可直接粘贴进 Excel）"""
        def _do_copy(_e=None):
            sel = tree.selection()
            if not sel:
                return
            lines = ['\t'.join(str(v) for v in tree.item(i, 'values')) for i in sel]
            self.root.clipboard_clear()
            self.root.clipboard_append('\n'.join(lines))
            self._set_status(f'已复制 {len(sel)} 行到剪贴板')
            return 'break'

        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label='复制选中行 (Ctrl+C)', command=_do_copy)

        def _popup(e):
            iid = tree.identify_row(e.y)
            if iid and iid not in tree.selection():
                tree.selection_set(iid)
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        tree.bind('<Button-3>', _popup)
        tree.bind('<Control-c>', _do_copy)

    # ---------- 数据源公共状态 ----------
    def _src_loading(self, src=None):
        """指定数据源（默认当前源）是否正在加载/刷新"""
        src = src or self.cur_src
        if src == SRC_BLOCK:
            return self.block_loading
        if src == SRC_MARKET:
            return self.market_loading
        if src == SRC_MARGIN:
            return self.margin_loading
        return self.loading or bool(self._preloading)

    def _update_refresh_btn_state(self):
        """按当前数据源的加载状态修正【立即刷新】按钮可用性：
        修复"某源刷新中切换到另一数据源后，按钮停留在禁用态、无法刷新"的问题"""
        self.refresh_btn.config(state='disabled' if self._src_loading() else 'normal')

    def _select_src(self, src):
        """快捷键/程序切换一级数据源"""
        if self.src_combo.get() == src:
            return
        self.src_combo.set(src)
        self._on_nav_change()

    # ---------- 重绘防抖 ----------
    def _schedule_chart_redraw(self):
        """窗口拖动/缩放会连续触发多次 Configure，防抖 60ms 只重绘一次"""
        if self._chart_job:
            self.root.after_cancel(self._chart_job)
        self._chart_job = self.root.after(60, self._redraw_charts)

    def _schedule_market_redraw(self):
        """大盘视图（表格+折线图）重绘防抖：按当前 Canvas 宽度拉伸列宽重绘"""
        if self._market_job:
            self.root.after_cancel(self._market_job)
        self._market_job = self.root.after(60, self._render_market)

    def _schedule_margin_redraw(self):
        """两融折线图重绘防抖（窗口缩放/拖动会连续触发 Configure）"""
        if self._margin_job:
            self.root.after_cancel(self._margin_job)
        self._margin_job = self.root.after(60, self._redraw_margin_charts)

    # ---------- 数据加载 ----------
    def _preload_one(self, idx):
        """后台预加载单个指数（三指数各自独立线程并行执行，先算好的先可用）。
        缓存覆盖到T-1即渲染级秒出（B方案），最新交易日T由后台增量补拉。"""
        if self._stop:
            return
        name = INDEX_NAMES[idx]
        # 登记预加载中，供 watchdog 让路判断（原实现未登记，导致 auto-update
        # 在指数预加载尚未完成时就误判空闲、提前并发，与预加载争用 thsdk/DB）
        self._preloading.add(idx)
        try:
            t0 = datetime.now()
            data = server.compute(idx, on_latest=self._on_latest_refresh)
            cost = (datetime.now() - t0).total_seconds()
            self.results[idx] = data
            self.idx_err.pop(name, None)
            _log(f'preload {name} OK {data["meta"]["period"]} '
                 f'n={data["meta"]["n"]} cost={cost:.1f}s')
            # 当前数据源是指数且就是该指数时，立即渲染
            if self.cur_src == SRC_INDEX and idx == self.cur_index:
                self._ui_call(lambda d=data: self._apply_result(d))
        except Exception as e:
            _log(f'preload {name} ERR {e}\n{traceback.format_exc()}')
            self._ui_call(lambda n=name, err=_err_text(e): self._idx_preload_fail(n, err))
        finally:
            self._preloading.discard(idx)
            self._ui_call(lambda n=name: self._preload_end(n))

    def _idx_preload_fail(self, name, err):
        """指数预加载失败：状态栏显式提示（原实现只写日志，界面永远停在"正在加载"）。"""
        self.idx_err[name] = err
        self._update_refresh_btn_state()
        if self.cur_src == SRC_INDEX and INDEX_NAMES[self.cur_index] == name:
            self._set_status(f'{name} 加载失败：{err}（可点【立即刷新】重试）')

    def _on_latest_refresh(self, data):
        """B方案回调：后台增量刷新完成，指数数据已更新到最新交易日，界面转主线程重绘。"""
        idx = data['meta']['index']
        name = data['meta']['name']
        self.results[idx] = data
        _log(f'latest-refresh {name} OK {data["meta"]["period"]}')

        def apply():
            if self._stop:
                return
            if self.cur_src == SRC_INDEX and idx == self.cur_index:
                self._apply_result(data)
            self._set_status(f'{name} 已刷新到最新交易日（{data["meta"]["period"]}）')

        self._ui_call(apply)

    def _preload_block(self):
        """启动预加载板块 OBV 逐日统计：缓存命中最新交易日则免联网秒出，
        否则自动联网更新到最新日期（约1-3分钟，后台执行不阻塞界面）。"""
        self.block_loading = True
        try:
            t0 = datetime.now()
            data = server.get_block_obv_daily(force=False)
            cost = (datetime.now() - t0).total_seconds()
            self.block_result = data
            self.block_err = None
            _log(f'preload block obv daily OK {data["meta"]["period"]} '
                 f'n={data["meta"]["n"]}/{data["meta"]["total"]} cost={cost:.1f}s')
            # 当前数据源是板块时立即渲染
            if self.cur_src == SRC_BLOCK:
                self._ui_call(lambda d=data: self._apply_result(d))
        except Exception as e:
            _log(f'preload block obv daily ERR {e}\n{traceback.format_exc()}')
            self._ui_call(lambda err=_err_text(e): self._block_preload_fail(err))
        finally:
            self.block_loading = False
            self._ui_call(lambda: self._preload_end('板块'))

    def _block_preload_fail(self, err):
        """板块预加载失败：状态栏显式提示。"""
        self.block_err = err
        self._update_refresh_btn_state()
        if self.cur_src == SRC_BLOCK:
            self._set_status(f'板块统计加载失败：{err}（可点【立即刷新】重试）')

    def _load_block_view(self, force=False):
        """板块源：后台计算板块指数 OBV 逐日统计（缓存秒出，首次联网较慢）。
        预加载已在进行时不重复开线程（避免双份网络请求与 DB 争用）"""
        if self.block_loading:
            return
        self.block_loading = True
        self._clear_view()
        self._set_status('正在统计板块 OBV 强势（首次约1-3分钟）…')

        def worker():
            try:
                data = server.get_block_obv_daily(force=force)
                _log(f'block obv daily OK {data["meta"]["period"]} '
                     f'n={data["meta"]["n"]}/{data["meta"]["total"]}')
                self._ui_call(lambda: self._block_view_done(data))
            except Exception as e:
                _log(f'block obv daily ERR {e}\n{traceback.format_exc()}')
                self._ui_call(lambda msg=_err_text(e): self._block_view_fail(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _block_view_done(self, data):
        self.block_loading = False
        self.block_result = data
        self._apply_result(data)

    def _block_view_fail(self, err):
        self.block_loading = False
        self._set_status(f'板块统计失败：{err}')

    def _preload_market(self):
        """启动预加载大盘每日情绪统计：缓存命中最新交易日则免联网秒出，
        首次联网逐日回填历史（约3-5分钟，后台执行不阻塞界面）。"""
        self.market_loading = True
        try:
            t0 = datetime.now()
            data = server.get_market_daily(force=False,
                                           progress_cb=self._market_progress)
            cost = (datetime.now() - t0).total_seconds()
            self.market_result = data
            self.market_err = None
            _log(f'preload market daily OK {data["meta"]["period"]} '
                 f'n={data["meta"]["n"]} cost={cost:.1f}s')
            if self.cur_src == SRC_MARKET:
                self._ui_call(lambda d=data: self._apply_result(d))
            # 成交总额缺失时后台回填，完成后自动刷新
            self._ensure_market_volume_backfill()
        except Exception as e:
            _log(f'preload market daily ERR {e}\n{traceback.format_exc()}')
            self._ui_call(lambda err=_err_text(e): self._market_preload_fail(err))
        finally:
            self.market_loading = False
            self._ui_call(lambda: self._preload_end('大盘'))

    def _market_preload_fail(self, err):
        """大盘预加载失败：画布与状态栏显式提示。

        原实现失败只写日志（且失败原因对用户不可见），界面永久停在空白表格 +
        "正在加载…"状态，用户完全无法判断是"还在加载"还是"已经失败"。
        """
        self.market_err = err
        self._update_refresh_btn_state()
        if self.cur_src == SRC_MARKET:
            self._market_key = None      # 令缓存键失效，强制重绘提示
            self._render_market()
        self._set_status(f'大盘统计加载失败：{err}（可点【立即刷新】重试）')

    def _preload_margin(self):
        """预加载两融融资余额：缓存命中全部交易日则免联网秒出，
        否则并发补齐缺失交易日（深市/京市只有单日接口，首次约1-3分钟）。"""
        self.margin_loading = True
        try:
            t0 = datetime.now()
            data = server.get_margin_daily(force=False, progress_cb=self._margin_progress)
            cost = (datetime.now() - t0).total_seconds()
            self.margin_result = data
            self.margin_err = None
            _log(f'preload margin daily OK {data["meta"]["period"]} '
                 f'n={data["meta"]["n"]} cost={cost:.1f}s')
            if self.cur_src == SRC_MARGIN:
                self._ui_call(lambda d=data: self._apply_result(d))
        except Exception as e:
            _log(f'preload margin daily ERR {e}\n{traceback.format_exc()}')
            self._ui_call(lambda err=_err_text(e): self._margin_preload_fail(err))
        finally:
            self.margin_loading = False
            self._ui_call(lambda: self._preload_end('两融'))

    def _margin_preload_fail(self, err):
        """两融预加载失败：状态栏显式提示。"""
        self.margin_err = err
        self._update_refresh_btn_state()
        if self.cur_src == SRC_MARGIN:
            self._set_status(f'两融统计加载失败：{err}（可点【立即刷新】重试）')

    def _margin_progress(self, done, total):
        """两融逐日补齐进度 -> 状态栏（节流 0.3s）"""
        if done < total:
            now = time.monotonic()
            if now - self._prog_ts < 0.3:
                return
            self._prog_ts = now
        msg = f'两融融资余额补齐中：{done}/{total} 个交易日…'
        self._ui_call(lambda m=msg: self._set_status(m))

    def _load_margin_view(self, force=False):
        """两融源：后台拉取沪深京融资余额（缓存秒出，首次联网较慢）"""
        if self.margin_loading:
            return
        self.margin_loading = True
        self._clear_view()
        self._set_status('正在拉取两融融资余额（首次约1-3分钟）…')

        def worker():
            try:
                data = server.get_margin_daily(force=force,
                                               progress_cb=self._margin_progress)
                _log(f'margin daily OK {data["meta"]["period"]} n={data["meta"]["n"]}')
                self._ui_call(lambda: self._margin_view_done(data))
            except Exception as e:
                _log(f'margin daily ERR {e}\n{traceback.format_exc()}')
                self._ui_call(lambda msg=_err_text(e): self._margin_view_fail(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _margin_view_done(self, data):
        self.margin_loading = False
        self.margin_result = data
        self._apply_result(data)

    def _margin_view_fail(self, err):
        self.margin_loading = False
        self._set_status(f'两融统计失败：{err}')

    def _market_progress(self, done, total):
        """问财逐日回填进度 -> 状态栏（线程内调用，经 UI 队列转主线程）。
        节流：0.3s 内多次回调合并，避免主线程消息队列堆积拖慢界面"""
        if done < total:
            now = time.monotonic()
            if now - self._prog_ts < 0.3:
                return
            self._prog_ts = now
        msg = f'大盘统计回填中：{done}/{total} 个交易日…'
        self._ui_call(lambda m=msg: self._set_status(m))

    def _load_market_view(self, force=False):
        """大盘源：后台拉取大盘每日情绪统计（缓存秒出，首次联网较慢）。
        预加载已在进行时不重复开线程（避免双份网络请求与 DB 争用）"""
        if self.market_loading:
            return
        self.market_loading = True
        self._clear_view()
        self._set_status('正在统计大盘情绪（首次回填约3-5分钟）…')

        def worker():
            try:
                data = server.get_market_daily(force=force,
                                               progress_cb=self._market_progress)
                _log(f'market daily OK {data["meta"]["period"]} n={data["meta"]["n"]}')
                self._ui_call(lambda: self._market_view_done(data))
            except Exception as e:
                _log(f'market daily ERR {e}\n{traceback.format_exc()}')
                self._ui_call(lambda msg=_err_text(e): self._market_view_fail(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _market_view_done(self, data):
        self.market_loading = False
        self.market_result = data
        self._apply_result(data)
        self._ensure_market_volume_backfill()

    def _market_view_fail(self, err):
        self.market_loading = False
        self.market_err = err
        self._market_key = None          # 缓存键失效，重绘为失败提示
        self._render_market()
        self._set_status(f'大盘统计失败：{err}')

    def _ensure_market_volume_backfill(self):
        """成交总额后台回填：get_market_daily 已改为只读缓存秒出，成交总额缺失时
        显示'-'。若缓存未覆盖最新交易日或存在历史缺口，后台线程补全，完成后自动
        刷新大盘视图。server 侧 request_volume_backfill 保证单线程幂等；这里仅做
        轻量防抖（10分钟内不重复触发），避免数据源故障时每次刷新都空转重试。"""
        if not server.market_volume_needs_backfill():
            return
        now = time.monotonic()
        if now - self._vol_backfill_ts < 600:
            return
        self._vol_backfill_ts = now
        _log('market volume: gap detected, start background backfill')

        def on_done():
            try:
                # 回填完成回调在后台线程：切回主线程重新拉取并刷新
                self._ui_call(self._on_market_volume_refreshed)
            except Exception:
                pass

        server.request_volume_backfill(on_done=on_done)

    def _on_market_volume_refreshed(self):
        """成交总额后台回填完成后：重新取大盘数据（缓存秒出）并刷新视图。"""
        try:
            data = server.get_market_daily(force=False)
            self.market_result = data
            if self.cur_src == SRC_MARKET:
                self._apply_result(data)
            _log('market volume backfill done, view refreshed')
            # 若回填后仍有缺口（部分日期接口失败），重置防抖时间戳，允许后续自动重试
            if server.market_volume_needs_backfill():
                self._vol_backfill_ts = 0.0
        except Exception as e:
            _log(f'market volume refresh ERR {e}\n{traceback.format_exc()}')

    # ---------- 每日自动更新 ----------
    def _margin_watchdog(self):
        """两融专项看门狗：最近交易日合计缺失时静默重试补齐。

        两融（沪深京融资余额）与行情不同步：当日数据要等交易所当晚陆续发布，
        且深交所/北交所接口偶发 SSL 断连。原实现只在启动预加载与每日自动更新
        里各试一次，失败即沿用 6 小时节流——程序常驻时，当天合计会一直显示 "-"。
        这里每 5 分钟只重拉"最近缺市日"（1~3 个请求），补齐后刷新界面。
        """
        while not self._stop:
            time.sleep(300)
            try:
                if self._stop:
                    return
                if self.margin_loading or self._preloading:
                    continue
                if not server.margin_recent_incomplete():
                    continue
                data = server.get_margin_daily(force=False)
                prev = self.margin_result
                if prev is not None and prev.get('days') == data['days']:
                    continue        # 无变化：不重绘、不覆盖状态栏
                self._ui_call(lambda d=data: self._margin_watchdog_apply(d))
                _log(f'margin watchdog refresh {data["meta"]["period"]}')
            except Exception as e:
                _log(f'margin watchdog ERR {e}')

    def _margin_watchdog_apply(self, data):
        """看门狗补齐后刷新界面：不动 margin_loading，避免与手动刷新抢状态。"""
        self.margin_result = data
        if self.cur_src == SRC_MARGIN:
            self._apply_result(data)

    def _auto_update_watchdog(self):
        """自动更新守护线程：把三大数据源刷新到最新完整交易日 T。

        原实现限定「工作日 15:10 之后」，且周末直接把 _auto_date 标为已处理，
        导致周六/周日或节假日打开程序时永不触发更新——节前最后一个交易日的数据
        过了整个假期都补不上（用户反馈"没有自动更新最新交易日"的根因）。
        改为数据缺口驱动：只要任一数据源未覆盖 T 就触发，与星期几无关。
        T 由 server._latest_trading_date() 判定（盘中返回上一交易日），
        因此盘中不会去拉尚未收盘的数据。数据已完整时降低复查频率避免空转。"""
        while not self._stop:
            try:
                # 有预加载/手动刷新在跑时先让路，避免并发争用 thsdk
                if (self._preloading or self.loading or self.block_loading
                        or self.market_loading or self.margin_loading):
                    time.sleep(30)
                    continue
                if self._auto_updating:      # 上一轮更新仍在进行
                    time.sleep(120)
                    continue
                if not self._data_stale():
                    # 三源均已覆盖最新交易日：标记当日已检查并低频复查
                    self._auto_date = datetime.now(TZ).date().isoformat()
                    time.sleep(600)
                    continue
                self._auto_date = datetime.now(TZ).date().isoformat()
                self._auto_updating = True
                threading.Thread(target=self._daily_auto_update, daemon=True).start()
                time.sleep(120)
            except Exception as e:
                _log(f'auto update watchdog ERR {e}\n{traceback.format_exc()}')
                time.sleep(120)

    def _data_stale(self):
        """各数据源是否有任一未覆盖最新完整交易日 T（需联网补齐）。

        成交总额（market_volume，上交所/深交所成交金额）是独立于大盘情绪统计的
        数据源：原实现只检查三指数+板块+大盘，三源到 T 后本函数返回 False，
        watchdog 便不再触发每日自动更新，而成交总额的唯一补齐入口
        _ensure_market_volume_backfill 只挂在自动更新里，于是成交总额一旦落后
        就永远停在旧交易日（用户反馈"成交总额没有自动更新"的根因）。
        判定失败按"不缺"处理，避免异常时空转反复重拉。"""
        try:
            for idx in INDEX_CODES:
                if not server.idx_cache_latest(idx):
                    return True
                # 基差是独立数据源（现货+期货），个股到 T 不代表基差也到 T。
                # 原实现不检查基差，个股已到 T 时本函数返回 False，watchdog 便
                # 不再触发自动更新，最新交易日基差会一直显示'-'。
                if server.basis_needs_refresh(idx):
                    return True
            if not server.block_cache_latest():
                return True
            if not server.market_cache_latest():
                return True
            if server.market_volume_needs_backfill():
                return True
            # 两融是独立数据源（沪深京融资余额），不检查则最新交易日会一直不补齐
            if server.margin_needs_refresh():
                return True
        except Exception as e:
            _log(f'data stale check ERR {e}')
            return False
        return False

    def _daily_auto_update(self):
        """串行刷新三大数据源到最新交易日。

        全程走各自内部的增量补齐，不使用 force=True 全量重拉：
        - 指数：compute + on_latest，只把未到 T 的成分股放进后台补拉队列；
        - 板块：get_block_obv_daily 内部按 meta 实际日期判定是否联网重建；
        - 大盘：get_market_daily 内部对未缓存交易日逐日回填。
        原实现在缓存落后时一律 force=True，中证2000 全量约 5 分钟、三源串行
        十几分钟，用户往往等不到就关闭程序，数据长期停在半补状态。"""
        try:
            self._ui_call(lambda: self._set_status('每日自动更新：正在检查缓存…'))
            # 1) 先把三指数基差一次性并行补齐到 T
            # 基差是独立数据源（现货+期货），不能依赖后台 worker 的串行重算循环。
            # 原实现在下面的循环里逐个串行调用 ensure_basis_fresh，每个 30-45 秒、
            # 合计约 2 分钟，用户中途关闭程序时后面的指数基差就永远停在旧交易日
            # （实测每次只有部分指数补到 T）。改为循环前一次性并行补齐，
            # 即使后续 compute 被打断，基差也已经全部到位。
            try:
                refreshed = server.refresh_all_basis(INDEX_CODES)
                for idx in INDEX_CODES:
                    if refreshed.get(idx):
                        _log(f'auto-update {INDEX_NAMES[idx]} basis refreshed to latest')
            except Exception as e:
                _log(f'auto-update basis ERR {e}\n{traceback.format_exc()}')
            # 2) 三个指数
            for idx in INDEX_CODES:
                if self._stop:
                    return
                name = INDEX_NAMES[idx]
                try:
                    # 统一走常规 compute + on_latest，由后台增量补拉只补未到 T 的
                    # 成分股。原实现在 idx_cache_latest=False 时用 force=True 全量
                    # 重拉：中证2000 全量约 5 分钟，三指数串行十几分钟，用户往往等
                    # 不到就关闭程序，数据永远只补一半（dist 库曾停在半补状态）。
                    # 已到 T 的成分股不会被重复拉取，未到的才进后台补拉队列。
                    data = server.compute(idx, on_latest=self._on_latest_refresh)
                    _log(f'auto-update {name} {data["meta"]["period"]} '
                         f'refreshing={data["meta"].get("latest_refreshing")}')
                    self.results[idx] = data
                    if self.cur_src == SRC_INDEX and idx == self.cur_index:
                        self._ui_call(lambda d=data: self._apply_result(d))
                except Exception as e:
                    _log(f'auto-update {name} ERR {e}\n{traceback.format_exc()}')
            # 3) 板块 OBV 逐日
            try:
                # get_block_obv_daily 内部已按 meta 记录的实际数据日期判定是否联网
                # 重建（含 thsdk 增量补当日行情），force=True 会无条件全量重拉，无必要
                bdata = server.get_block_obv_daily(force=False)
                _log(f'auto-update block {bdata["meta"]["period"]}')
                self.block_result = bdata
                if self.cur_src == SRC_BLOCK:
                    self._ui_call(lambda d=bdata: self._apply_result(d))
            except Exception as e:
                _log(f'auto-update block ERR {e}\n{traceback.format_exc()}')
            # 4) 大盘情绪统计
            try:
                # get_market_daily 内部对未缓存的交易日逐日回填（force=False 也补），
                # force=True 只是额外重查最新交易日；自动更新场景无需强制重查
                mdata = server.get_market_daily(force=False,
                                                progress_cb=self._market_progress)
                _log(f'auto-update market {mdata["meta"]["period"]}')
                self.market_result = mdata
                if self.cur_src == SRC_MARKET:
                    self._ui_call(lambda d=mdata: self._apply_result(d))
                # 成交总额缺失时后台回填，完成后自动刷新
                self._ensure_market_volume_backfill()
            except Exception as e:
                _log(f'auto-update market ERR {e}\n{traceback.format_exc()}')
            # 5) 两融融资余额（沪深京）
            try:
                rdata = server.get_margin_daily(force=False,
                                                progress_cb=self._margin_progress)
                _log(f'auto-update margin {rdata["meta"]["period"]}')
                self.margin_result = rdata
                if self.cur_src == SRC_MARGIN:
                    self._ui_call(lambda d=rdata: self._apply_result(d))
            except Exception as e:
                _log(f'auto-update margin ERR {e}\n{traceback.format_exc()}')
            self._ui_call(lambda: self._set_status(
                f'每日自动更新完成（{datetime.now(TZ).strftime("%H:%M")}）'))
        except Exception as e:
            _log(f'daily auto update fatal ERR {e}\n{traceback.format_exc()}')
        finally:
            # 必须复位：否则 watchdog 会永久认为"上一轮更新仍在跑"而不再复查
            self._auto_updating = False

    # ---------- 切换联动 ----------
    def _on_nav_change(self):
        """一级数据源下拉：指数 <-> 板块 <-> 大盘 <-> 两融，切换主体区域显示"""
        src = self.src_combo.get()
        frames = {SRC_INDEX: self.idx_frame, SRC_BLOCK: self.block_frame,
                  SRC_MARKET: self.market_frame, SRC_MARGIN: self.margin_frame}
        self.cur_src = src
        for s, frame in frames.items():
            if s == src:
                frame.pack(fill='both', expand=True)
            else:
                frame.pack_forget()
        # 按当前源的加载状态修正【立即刷新】按钮（修复"某源刷新中切到另一源后
        # 按钮停留在禁用态"的问题）
        self._update_refresh_btn_state()
        if src == SRC_MARGIN:
            if self.margin_result is not None:
                self._apply_result(self.margin_result)
            elif self.margin_loading:
                self._set_status('两融统计加载中，请稍候…')
            else:
                self._load_margin_view()
        elif src == SRC_BLOCK:
            if self.block_result is not None:
                # 启动预加载已完成：直接渲染缓存，避免重复计算
                self._apply_result(self.block_result)
            elif self.block_loading:
                # 预加载进行中：等它完成后会自动渲染，不重复开线程
                self._set_status('板块统计加载中，请稍候…')
            else:
                self._load_block_view()
        elif src == SRC_MARKET:
            if self.market_result is not None:
                self._apply_result(self.market_result)
            elif self.market_loading:
                self._set_status('大盘统计加载中，请稍候…')
            else:
                self._load_market_view()
        else:
            self._on_idx_tab_changed()

    def _on_idx_tab_changed(self, event=None):
        """指数区子选项卡切换：更新当前指数并渲染（预加载缓存零等待）"""
        if self.cur_src != SRC_INDEX:
            return
        ti = self.idx_notebook.index(self.idx_notebook.select())
        self.cur_index = INDEX_CODES[ti]
        result = self.results.get(self.cur_index)
        name = INDEX_NAMES[self.cur_index]
        if result is not None:
            self._apply_result(result)
        else:
            self._clear_view()
            err = self.idx_err.get(name) or self.db_err
            if err:
                self._set_status(f'{name} 加载失败：{err}（可点【立即刷新】重试）')
            else:
                self._set_status(f'{name} 正在加载，请稍候…')

    # ---------- 渲染 ----------
    def _apply_result(self, data):
        self.loading = False
        self._update_refresh_btn_state()
        self._render()
        msg = (f'{data["meta"]["name"]}  {data["meta"]["period"]}  '
               f'{data["meta"]["n"]}/{data["meta"]["total"]}')
        if self.cur_src == SRC_MARGIN:
            hint = self._margin_hint(data)
            if hint:
                msg = f'{msg} | {hint}'
        self._set_status(msg)

    def _margin_hint(self, result):
        """最新交易日合计缺失时给出原因说明，避免被误读成"两融数据没更新"。

        两融数据当晚才陆续发布，且沪深京接口偶发失败：此时该日合计为 "-" 属正常，
        后台会自动重试补齐。原实现无任何提示，用户只能看到最新一天是 "-"。
        """
        days = (result or {}).get('days') or []
        if not days:
            return None
        last = days[-1]
        if last.get('total') is not None:
            return None
        miss = [n for n, v in (('沪市', last.get('sh')), ('深市', last.get('sz')),
                               ('京市', last.get('bj'))) if v is None]
        if len(miss) == 3:
            return f'{last["date"]} 三市数据尚未发布，后台自动重试中'
        return f'{last["date"]} {"、".join(miss)}数据缺失，后台自动重试中'

    def _render(self):
        if self.cur_src == SRC_MARKET:
            self._render_market()
            return
        if self.cur_src == SRC_MARGIN:
            self._render_margin()
            return
        view = self._current_view()
        view['main'].delete(*view['main'].get_children())
        if 'detail' in view:
            view['detail'].delete(*view['detail'].get_children())
        result = self._current_result()
        if result is None:
            return
        if self.cur_src == SRC_BLOCK:
            # 板块视图：三列每日统计（最新日期在最上面）
            for day in reversed(result['days']):
                view['main'].insert('', 'end',
                                    values=(day['date'], day['obv_strong'],
                                            day['obv_strong_above60']))
            self._stripe(view['main'])
            self._reset_sort(view['main'])
            # 默认选中最新一行（倒序后为第一行），联动显示下方板块名称区
            children = view['main'].get_children()
            if children:
                view['main'].selection_set(children[0])
                view['main'].see(children[0])
            self._show_block_names()
            self._redraw_block_charts()
            return
        for day in reversed(result['days']):
            b = day.get('basis')
            basis_txt = '-' if b is None else f'{b:.2f}'
            view['main'].insert('', 'end',
                                values=(day['date'], len(day['high']), len(day['low']),
                                        basis_txt))
        self._stripe(view['main'])
        self._reset_sort(view['main'])
        # 默认选中最新交易日（倒序后第一行），联动显示个股明细
        # （原实现明细区初始空白，用户不点一行根本不知道这里能看个股）
        children = view['main'].get_children()
        if children:
            view['main'].selection_set(children[0])
            view['main'].see(children[0])
        self._show_detail()
        self._redraw_charts()

    def _render_market(self):
        """大盘视图：12列每日情绪统计表直接绘制在 Canvas 上
        （表头深色，数据行红绿着色，最新日期在最上面，隔行底色）"""
        if self.cur_src != SRC_MARKET:
            return
        view = self._current_view()
        canvas = view['canvas']
        result = self._current_result()
        if result is None:
            # 数据未就绪/已失败：在表格区绘制可读提示。
            # 原实现直接清空画布，用户看到纯白区域无法区分"正在加载"与"加载失败"。
            # 同时清掉旧行命中数据：否则加载失败后悬停高亮/点击仍会命中
            # 上一次渲染的日期行，弹出已不属于当前状态的明细
            self._market_days = None
            if canvas.winfo_width() < 2 or canvas.winfo_height() < 2:
                self.root.after(80, self._render_market)
                return
            ph_key = ('placeholder', canvas.winfo_width(), canvas.winfo_height(),
                      self.market_loading, self.market_err, self.db_err)
            if ph_key != self._market_key:
                canvas.delete('all')
                self._draw_market_placeholder(canvas)
                self._market_key = ph_key
            return
        # 数据对象与画布尺寸均未变化时跳过重绘（窗口拖动不再反复全量重画）。
        # 注意：这里必须叫 cache_key。原实现用 key 命名，随后被下方列元组的解包变量
        # key 覆盖，导致最后写入 self._market_key 的是最后一个列名（'high_lb'），
        # 缓存键永远匹配不上，去重优化彻底失效、每次刷新都全量重绘。
        cache_key = (id(result), canvas.winfo_width(), canvas.winfo_height())
        if cache_key == self._market_key:
            return
        if canvas.winfo_width() < 2 or canvas.winfo_height() < 2:
            # 首次启动窗口尚未完成布局时 winfo_width 会返回 1（未映射），
            # 此时绘制内容落在不可见区域导致空白；延迟到布局完成后重绘
            self.root.after(80, self._render_market)
            return
        canvas.delete('all')
        self._market_hover_row = -1
        canvas.config(cursor='')
        F = ('Microsoft YaHei UI', 10)
        FB = ('Microsoft YaHei UI', 10, 'bold')
        HEAD_H, ROW_H = MARKET_HEAD_H, MARKET_ROW_H
        total_w = sum(w for _, _, w, _ in MARKET_COLS)
        days = list(reversed(result['days']))  # 最新在最上方
        self._market_days = days  # 供行点击命中检测（最新在索引0）
        total_h = HEAD_H + ROW_H * len(days)
        # 窗口比表格宽时按比例拉伸列宽铺满，避免右侧留白
        cw = canvas.winfo_width()
        factor = max(1.0, cw / total_w) if total_w else 1.0
        xs, widths, x = [], [], 0.0
        for _, _, w, _ in MARKET_COLS:
            cw0 = w * factor
            xs.append(x)
            widths.append(cw0)
            x += cw0
        # 表头行（先画深色背景矩形，再画白色表头文字；缺背景矩形会让白字溶于白底）
        canvas.create_rectangle(0, 0, x, HEAD_H, fill=COLOR_HEADER, outline='')
        for (col_key, head, _w, _fg), x0, cw0 in zip(MARKET_COLS, xs, widths):
            canvas.create_text(x0 + cw0 / 2, HEAD_H / 2, text=head,
                               fill='white', font=FB)
        # 数据行，隔行浅灰底
        for r, day in enumerate(days):
            y0 = HEAD_H + r * ROW_H
            bg = 'white' if r % 2 == 0 else '#f2f5f8'
            canvas.create_rectangle(0, y0, x, y0 + ROW_H, fill=bg, outline='')
            prev_vol = days[r + 1].get('vol') if r + 1 < len(days) else None
            for (col_key, _head, _w, _fg), x0, cw0 in zip(MARKET_COLS, xs, widths):
                v = day.get(col_key)
                if col_key == 'vol':
                    txt = '-' if v is None else f'{v:.2f}'
                elif col_key == 'date':
                    parts = str(v).split('-')
                    txt = (f'{int(parts[0])}/{int(parts[1])}/{int(parts[2])}'
                           if len(parts) == 3 else str(v))
                else:
                    txt = '-' if v is None else str(v)
                # 红盘/绿盘数量超过 3000 时给单元格加底色
                try:
                    n = int(v)
                    if col_key == 'red' and n > 3000:
                        canvas.create_rectangle(x0, y0, x0 + cw0, y0 + ROW_H,
                                                fill='#ffd9d9', outline='')
                    elif col_key == 'green' and n > 3000:
                        canvas.create_rectangle(x0, y0, x0 + cw0, y0 + ROW_H,
                                                fill='#c9ecc9', outline='')
                    elif col_key == 'lb':
                        canvas.create_rectangle(x0, y0, x0 + cw0, y0 + ROW_H,
                                                fill='#fff59d', outline='')
                except (TypeError, ValueError):
                    pass
                # 非一字涨停=红字，跌停=绿字，其余列黑字；成交总额比昨日增红减绿
                if col_key == 'vol' and v is not None and prev_vol is not None:
                    if v > prev_vol:
                        fill = COLOR_RED
                    elif v < prev_vol:
                        fill = COLOR_LOW
                    else:
                        fill = 'black'
                elif col_key == 'fzt':
                    fill = COLOR_RED
                elif col_key == 'dt':
                    fill = COLOR_LOW
                else:
                    fill = 'black'
                canvas.create_text(x0 + cw0 / 2, y0 + ROW_H / 2, text=txt,
                                   fill=fill, font=F)
        canvas.configure(scrollregion=(0, 0, x, total_h))
        canvas.yview_moveto(0)
        self._market_width = x
        self._market_key = cache_key
        self._redraw_market_charts()

    def _draw_market_placeholder(self, canvas):
        """数据未就绪时在表格区显示状态提示：加载中 / 加载失败（含原因）/ 暂无数据。"""
        w = max(canvas.winfo_width(), 200)
        if self.db_err:
            title, sub, color = '数据库初始化失败', self.db_err, COLOR_RED
        elif self.market_err:
            title, sub, color = '数据加载失败', f'{self.market_err}（可点【立即刷新】重试）', COLOR_RED
        elif self.market_loading:
            title, sub, color = '正在加载数据…', '首次打开或需联网补齐时约 1~3 分钟，请稍候', COLOR_HEADER
        else:
            title, sub, color = '暂无数据', '请点击工具栏【立即刷新】', COLOR_HEADER
        canvas.create_text(w / 2, 48, text=title,
                           font=('Microsoft YaHei UI', 12, 'bold'), fill=color)
        canvas.create_text(w / 2, 78, text=sub,
                           font=('Microsoft YaHei UI', 9), fill='#666666')
        canvas.configure(scrollregion=(0, 0, w, 110))

    def _on_market_row_click(self, event):
        """大盘表格行点击：按 y 坐标命中数据行，弹出当日涨停/跌停/炸板/连板明细窗口"""
        if self.cur_src != SRC_MARKET or not getattr(self, '_market_days', None):
            return
        canvas = self._current_view()['canvas']
        y = canvas.canvasy(event.y)  # 换算画布坐标，兼容纵向滚动
        HEAD_H, ROW_H = MARKET_HEAD_H, MARKET_ROW_H
        if y < HEAD_H:
            return
        r = int((y - HEAD_H) // ROW_H)
        if 0 <= r < len(self._market_days):
            self._show_market_detail(self._market_days[r]['date'])

    def _on_market_hover(self, event):
        """大盘表格行悬停高亮 + 手型光标：提示整行可点击查看当日明细。
        高亮矩形画在文字之上、用 50% 抖动位图（stipple）半透明着色，
        行内文字仍清晰可读。"""
        if self.cur_src != SRC_MARKET:
            return
        canvas = self._current_view()['canvas']
        if event is None or not getattr(self, '_market_days', None):
            self._market_hover_clear(canvas)
            return
        y = canvas.canvasy(event.y)
        r = int((y - MARKET_HEAD_H) // MARKET_ROW_H) if y >= MARKET_HEAD_H else -1
        if not (0 <= r < len(self._market_days)):
            self._market_hover_clear(canvas)
            return
        if r == self._market_hover_row:
            return
        self._market_hover_row = r
        canvas.delete('hover')
        y0 = MARKET_HEAD_H + r * MARKET_ROW_H
        canvas.create_rectangle(0, y0, max(self._market_width, canvas.winfo_width()),
                                y0 + MARKET_ROW_H, fill='#bcd4ea', outline='#7f9cc0',
                                stipple='gray50', tags=('hover',))
        canvas.config(cursor='hand2')

    def _market_hover_clear(self, canvas):
        """清除悬停高亮（移出表格/数据重绘时）"""
        if self._market_hover_row != -1:
            self._market_hover_row = -1
            canvas.delete('hover')
            canvas.config(cursor='')

    def _show_market_detail(self, date_str):
        """大盘某交易日明细弹窗：涨停/连板/跌停/炸板四页签（连板=涨停中连续涨停>=2）。
        后台线程查 server.get_market_day_detail（缓存命中秒出，首次联网较慢）"""
        win = tk.Toplevel(self.root)
        win.title(f'大盘明细 {date_str}')
        win.geometry('780x540')
        win.transient(self.root)
        nb = ttk.Notebook(win)
        nb.pack(fill='both', expand=True, padx=8, pady=(8, 2))
        spec = [('涨停', ('code', 'name', 'lb', 'xtype')),
                ('连板', ('code', 'name', 'lb')),
                ('跌停', ('code', 'name')),
                ('炸板', ('code', 'name'))]
        headers = {'code': '代码', 'name': '名称', 'lb': '连板天数', 'xtype': '涨停类型'}
        widths = {'code': 100, 'name': 130, 'lb': 90, 'xtype': 200}
        trees = {}
        for label, cols in spec:
            f = tk.Frame(nb, bg=COLOR_PANEL)
            nb.add(f, text=label)
            tv = ttk.Treeview(f, columns=cols, show='headings')
            for c in cols:
                tv.heading(c, text=headers[c])
                tv.column(c, width=widths[c], anchor='center')
            vsb = ttk.Scrollbar(f, orient='vertical', command=tv.yview)
            tv.configure(yscrollcommand=vsb.set)
            tv.pack(side='left', fill='both', expand=True)
            vsb.pack(side='right', fill='y')
            tv.tag_configure('odd', background='#f2f5f8')
            self._attach_sort(tv)
            self._attach_copy_menu(tv)
            trees[label] = tv
        status = tk.Label(win, text='正在加载明细（首次联网约10秒）…', anchor='w',
                          bg=COLOR_TOOLBAR, fg=COLOR_TEXT,
                          font=('Microsoft YaHei UI', 9))
        status.pack(fill='x', side='bottom')

        def _on_win_close():
            # 弹窗销毁前清掉其表格的排序状态：字典以控件为键会一直引用已销毁
            # 的 Treeview，长开会话反复开弹窗会缓慢累积内存
            try:
                for tv in trees.values():
                    self._sort_state.pop(tv, None)
                    self._sort_orig.pop(tv, None)
            except Exception:
                pass
            win.destroy()

        win.protocol('WM_DELETE_WINDOW', _on_win_close)

        def _load():
            try:
                d = server.get_market_day_detail(date_str)
            except Exception:
                _log(f'market_detail {date_str} ERR\n{traceback.format_exc()}')
                d = None
            self._ui_call(lambda: _fill(d))

        def _fill(d):
            # 弹窗可能在后台查询期间被用户关闭：此时 treeview/status 已被销毁，
            # 再访问会抛 TclError("invalid command name")。先判活，避免异步回调竞态。
            if not win.winfo_exists():
                return
            if d is None:
                status.config(text=f'{date_str} 明细查询失败，请稍后重试')
                return
            if d.get('skip'):
                status.config(text=f'{date_str} 非交易日，无明细数据')
                return
            for zt in d['zt']:
                trees['涨停'].insert('', 'end',
                                     values=(zt['code'], zt['name'], zt['lb_days'], zt['xtype']))
            for zt in sorted((x for x in d['zt'] if x['lb_days'] >= 2),
                             key=lambda x: (-x['lb_days'], x['code'])):
                trees['连板'].insert('', 'end',
                                     values=(zt['code'], zt['name'], zt['lb_days']))
            for it in d['dt']:
                trees['跌停'].insert('', 'end', values=(it['code'], it['name']))
            for it in d['zb']:
                trees['炸板'].insert('', 'end', values=(it['code'], it['name']))
            for tv in trees.values():
                self._stripe(tv)
                self._reset_sort(tv)
            status.config(text=f'{date_str}：涨停 {len(d["zt"])} / 跌停 {len(d["dt"])} / 炸板 {len(d["zb"])} 只')

        threading.Thread(target=_load, daemon=True).start()

    # ---------- 折线图 ----------
    def _redraw_market_charts(self, event=None):
        """大盘视图两张折线图：最高板（非ST）+ 连板家数（左=最早，右=最新）"""
        if self.cur_src != SRC_MARKET:
            return
        view = self._current_view()
        if 'chart_hlb' not in view or 'chart_lb' not in view:
            return
        result = self._current_result()
        ch, cl = view['chart_hlb'], view['chart_lb']
        ch.delete('all')
        cl.delete('all')
        self._chart_meta.pop(ch, None)
        self._chart_meta.pop(cl, None)
        if result is None or not result['days']:
            return
        # 折线图画布尚未完成布局（首次启动）时延迟重绘，避免空白
        if view['chart_hlb'].winfo_width() < 60 or view['chart_hlb'].winfo_height() < 60:
            self.root.after(80, self._redraw_market_charts)
            return
        days = list(result['days'])[-self.chart_days:]
        dates = [d['date'] for d in days]
        hlb = [d.get('high_lb', 0) for d in days]
        lb = [d.get('lb', 0) for d in days]
        self._draw_line(ch, dates, [hlb], ['最高板'],
                        ['#d0342c'], title='最高板（非ST）', floor_zero=True)
        self._draw_line(cl, dates, [lb], ['连板'],
                        ['#1f4e9c'], title='连板家数', floor_zero=True)

    def _on_chart_days_change(self):
        """图表周期切换：30/60/90 个交易日或全部，作用于四个数据源的全部折线图"""
        v = self.chart_days_combo.get()
        self.chart_days = {'30日': 30, '60日': 60, '90日': 90}.get(v, 10 ** 9)
        self._redraw_charts()
        self._redraw_block_charts()
        self._redraw_margin_charts()
        self._redraw_market_charts()

    def _schedule_block_redraw(self):
        """板块折线图随窗口缩放重绘（60ms 防抖，同其他图表策略一致）"""
        if self._block_job is not None:
            self.root.after_cancel(self._block_job)
        self._block_job = self.root.after(60, self._redraw_block_charts)

    def _redraw_block_charts(self):
        """板块 OBV 趋势：剔除后强势数 / 被剔除数（站上60日线）"""
        result = self.block_result
        view = self.block_view
        c1, c2 = view['chart_strong'], view['chart_above60']
        c1.delete('all')
        c2.delete('all')
        self._chart_meta.pop(c1, None)
        self._chart_meta.pop(c2, None)
        if result is None or not result['days']:
            return
        days = list(result['days'])[-self.chart_days:]
        dates = [d['date'] for d in days]
        self._draw_line(c1, dates, [[d['obv_strong'] for d in days]],
                        ['OBV强势数(剔除后)'], [COLOR_RED],
                        'OBV强势板块数（剔除60日线上方）', floor_zero=True, area=True)
        self._draw_line(c2, dates, [[d['obv_strong_above60'] for d in days]],
                        ['被剔除数'], [COLOR_HIGH],
                        '被剔除板块数（站上60日线）', floor_zero=True, area=True)

    def _redraw_charts(self, event=None):
        """指数子选项卡内两张折线图：创新高/新低数量（红/绿）+ 基差"""
        if self.cur_src == SRC_BLOCK:
            return
        view = self._current_view()
        if 'chart1' not in view or 'chart2' not in view:
            return
        result = self._current_result()
        c1, c2 = view['chart1'], view['chart2']
        c1.delete('all')
        c2.delete('all')
        self._chart_meta.pop(c1, None)
        self._chart_meta.pop(c2, None)
        if result is None or not result['days']:
            return
        # 折线图 x 轴按日期正序（左=最早，右=最新）；显示窗口由工具栏 chart_days 决定；表格仍保持最新在最上方
        days = list(result['days'])[-self.chart_days:]
        dates = [d['date'] for d in days]
        highs = [len(d['high']) for d in days]
        lows = [len(d['low']) for d in days]
        self._draw_line(c1, dates, [highs, lows], ['创新高', '创新低'],
                        ['#d0342c', '#1e8a3c'], title='创新高 / 创新低数量',
                        floor_zero=True)
        v_dates, bases = [], []
        for d in days:
            b = d.get('basis')
            if b is not None:
                v_dates.append(d['date'])
                bases.append(b)
        self._draw_line(c2, v_dates, [bases], ['基差'],
                        ['#1f4e9c'], title='基差')

    def _render_margin(self):
        """两融视图：沪深京融资余额逐日表（最新在最上）+ 两张折线图"""
        result = self.margin_result
        view = self.margin_view
        view['main'].delete(*view['main'].get_children())
        if result is None:
            return
        for day in reversed(result['days']):
            chg = day['chg']
            tag = ('up',) if (chg or 0) > 0 else (('down',) if (chg or 0) < 0 else ())
            view['main'].insert('', 'end', tags=tag,
                                values=(day['date'], _fmt_amount(day['sh']),
                                        _fmt_amount(day['sz']), _fmt_amount(day['bj']),
                                        _fmt_amount(day['total']),
                                        _fmt_amount(chg, signed=True)))
        self._stripe(view['main'])
        self._reset_sort(view['main'])
        self._redraw_margin_charts()

    def _redraw_margin_charts(self):
        """两融折线图：左=三市合计融资余额走势，右=较前一日增减"""
        result = self.margin_result
        view = self.margin_view
        c1, c2 = view['chart1'], view['chart2']
        c1.delete('all')
        c2.delete('all')
        self._chart_meta.pop(c1, None)
        self._chart_meta.pop(c2, None)
        if result is None or not result['days']:
            return
        days = list(result['days'])[-self.chart_days:]
        t_dates, totals = [], []
        g_dates, chgs = [], []
        for d in days:
            if d['total'] is not None:
                t_dates.append(d['date'])
                totals.append(d['total'])
            if d['chg'] is not None:
                g_dates.append(d['date'])
                chgs.append(d['chg'])
        self._draw_line(c1, t_dates, [totals], ['合计融资余额'],
                        ['#1f4e9c'], title='沪深京融资余额合计（亿元）')
        self._draw_line(c2, g_dates, [chgs], ['较前一日'],
                        ['#d0342c'], title='融资余额日增减（亿元）', bold_zero=True)

    def _draw_line(self, canvas, dates, series_list, labels, colors, title='',
                   floor_zero=False, bold_zero=False, area=False):
        """在 Canvas 上绘制单张折线图（传统 Windows 风格：白底、灰色网格、坐标轴）

        floor_zero=True 用于数量类指标（创新高/新低数量、最高板、连板家数）：
        纵轴下界固定为 0。原实现统一在最小值下留 8% 余量，当最小值为 0 时
        会算出负刻度（如 -8），而数量不可能为负。

        area=True 且单序列时，在折线下方填充淡色面积区域（25% 抖动半透明）。
        所有图统一带末点高亮圆点 + 最新数值标签。
        """
        # 旧悬停元数据先行失效：本次未成功绘制（尺寸不足/无数据）时，
        # 避免悬停提示引用过期的坐标变换与数值
        self._chart_meta.pop(canvas, None)
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 60 or h < 60:
            return
        ml, mr, mt, mb = 44, 12, 24, 26
        pw, ph = w - ml - mr, h - mt - mb
        if pw <= 0 or ph <= 0:
            return
        if title:
            canvas.create_text(w / 2, 12, text=title,
                               font=('Microsoft YaHei UI', 9, 'bold'), fill='#333333')
        all_vals = [v for s in series_list for v in s]
        if not all_vals or not dates:
            return
        vmin, vmax = min(all_vals), max(all_vals)
        if vmin == vmax:
            vmin -= 1
            vmax += 1
        pad = (vmax - vmin) * 0.08
        vmin -= pad
        vmax += pad
        if floor_zero:
            vmin = 0.0
        n = len(dates)
        for i in range(6):
            y = mt + ph - ph * i / 5
            val = vmin + (vmax - vmin) * i / 5
            # bold_zero：仅把 0 刻度的文字加粗加深，让它更醒目（不是 0 轴，不动网格线）
            is_zero = bold_zero and abs(val) < max(abs(vmax), abs(vmin)) * 1e-6 + 1e-9
            canvas.create_line(ml, y, w - mr, y, fill='#e8e8e8')
            canvas.create_text(ml - 5, y, text=f'{val:.0f}', anchor='e',
                               font=('Microsoft YaHei UI', 9, 'bold') if is_zero
                               else ('Microsoft YaHei UI', 8),
                               fill='#333333' if is_zero else '#666666')
        # 0 位置的粗基准线 + 加粗的 0 标签（在折线之前画，避免盖住曲线）
        if bold_zero and vmin < 0 < vmax:
            span = vmax - vmin
            y0 = mt + ph - ph * (0 - vmin) / span
            canvas.create_line(ml, y0, w - mr, y0, fill='#999999', width=1.6)
            on_scale = any(abs(vmin + span * i / 5) < span * 0.02 for i in range(6))
            if not on_scale:      # 等分刻度未必落在 0 上（如 -358/…/188），补标一个
                canvas.create_text(ml - 5, y0, text='0', anchor='e',
                                   font=('Microsoft YaHei UI', 9, 'bold'), fill='#333333')
        # x 轴日期刻度：短格式(8-26)+刻度短线，均匀抽稀且首尾必取，避免拥挤
        def _fmt_date(d):
            s = str(d)
            parts = s.split('-') if '-' in s else []
            if len(parts) == 3:
                return f'{int(parts[1])}-{int(parts[2])}'
            return s

        max_labels = 7
        if n <= max_labels:
            idxs = list(range(n))
        else:
            idxs = sorted(set([0, n - 1] +
                              [int(round((n - 1) * k / (max_labels - 1)))
                               for k in range(1, max_labels - 1)]))
        for i in idxs:
            x = ml + pw * i / (n - 1) if n > 1 else ml + pw / 2
            canvas.create_line(x, h - mb, x, h - mb + 4, fill='#999999')
            canvas.create_text(x, h - mb + 10, text=_fmt_date(dates[i]),
                               font=('Microsoft YaHei UI', 8), fill='#4a5a6a')
        canvas.create_line(ml, mt, ml, h - mb, fill='#999999')
        canvas.create_line(ml, h - mb, w - mr, h - mb, fill='#999999')
        for series, label, color in zip(series_list, labels, colors):
            pts = []
            for i, v in enumerate(series):
                x = ml + pw * i / (n - 1) if n > 1 else ml + pw / 2
                y = mt + ph - ph * (v - vmin) / (vmax - vmin)
                pts.extend([x, y])
            if area and len(series_list) == 1 and len(pts) >= 4:
                # 面积填充：折线下方到 x 轴的淡色区域；先画面积再画折线，
                # 25% 抖动半透明，网格线与轴线仍可见
                poly = [pts[0], h - mb] + pts + [pts[-2], h - mb]
                canvas.create_polygon(*poly, fill=color, outline='', stipple='gray25')
            if len(pts) >= 4:
                canvas.create_line(*pts, fill=color, width=1.6, smooth=True)
        # 末点高亮 + 最新值标签：最新交易日数值一眼可读，不必再对照表格
        if n >= 2:
            for series, color in zip(series_list, colors):
                if not series:
                    continue
                v = series[-1]
                x = ml + pw
                y = mt + ph - ph * (v - vmin) / (vmax - vmin)
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline='white')
                ty = y - 12 if y > mt + 22 else y + 12
                canvas.create_text(x - 8, ty, text=self._fmt_chart_val(v), anchor='e',
                                   font=('Microsoft YaHei UI', 9, 'bold'), fill=color)
        lx, ly = ml + 6, mt + 4
        for label, color in zip(labels, colors):
            canvas.create_line(lx, ly + 4, lx + 18, ly + 4, fill=color, width=2)
            canvas.create_text(lx + 22, ly + 4, text=label, anchor='w',
                               font=('Microsoft YaHei UI', 8), fill='#333333')
            lx += 22 + len(label) * 12 + 12
        # 悬停取值元数据：注册坐标变换与序列值，供 _on_chart_motion 绘制
        # 十字线 + 数值面板（所有走 _draw_line 的折线图自动获得该能力）
        self._chart_meta[canvas] = {
            'dates': dates, 'series': list(zip(series_list, labels, colors)),
            'ml': ml, 'mt': mt, 'pw': pw, 'ph': ph,
            'vmin': vmin, 'vmax': vmax,
        }
        canvas.bind('<Motion>', self._on_chart_motion)
        canvas.bind('<Leave>', lambda e, c=canvas: self._on_chart_leave(c))

    # ---------- 折线图悬停取值 ----------
    @staticmethod
    def _fmt_chart_val(v):
        """悬停数值格式：整数不带小数点，其余保留2位（千分位）"""
        if v is None:
            return '-'
        if abs(v - round(v)) < 1e-9:
            return f'{round(v):,d}'
        return f'{v:,.2f}'

    def _on_chart_leave(self, canvas):
        """移出折线图：清除十字线与数值面板"""
        canvas.delete('tooltip')

    def _on_chart_motion(self, event):
        """折线图悬停：竖向十字线吸附最近交易日 + 各序列标记点 + 数值面板。
        原来看图只能目测网格估算数值，复盘时想确认某个具体日期的数值
        只能对照表格逐行找；悬停即读数后（新高达 32 只这类问题）一眼可得。"""
        canvas = event.widget
        meta = self._chart_meta.get(canvas)
        if not meta:
            return
        dates = meta['dates']
        n = len(dates)
        if not n:
            return
        f = self._tip_font
        if f is None:
            return
        ml, mt, pw, ph = meta['ml'], meta['mt'], meta['pw'], meta['ph']
        span = meta['vmax'] - meta['vmin']
        if span <= 0:
            return
        if n > 1:
            i = int(round((event.x - ml) / pw * (n - 1)))
            i = max(0, min(n - 1, i))
            x = ml + pw * i / (n - 1)
        else:
            i, x = 0, ml + pw / 2
        canvas.delete('tooltip')
        canvas.create_line(x, mt, x, mt + ph, fill='#4a6ea9', dash=(2, 3),
                           tags=('tooltip',))
        lines = [str(dates[i])]
        for series, label, color in meta['series']:
            v = series[i] if i < len(series) else None
            if v is not None:
                y = mt + ph - ph * (v - meta['vmin']) / span
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color,
                                   outline='white', tags=('tooltip',))
            lines.append(f'{label}：{self._fmt_chart_val(v)}')
        # 数值面板放绘图区右上角（左上角有图例，跟随光标则容易被画布边缘裁剪）
        lh = f.metrics('linespace') + 2
        bw = max(f.measure(s) for s in lines) + 16
        bx = max(ml + 4, ml + pw - bw - 8)
        by = mt + 6
        canvas.create_rectangle(bx, by, bx + bw, by + lh * len(lines) + 6,
                                fill='#fffef2', outline='#b8b89a', tags=('tooltip',))
        for k, s in enumerate(lines):
            canvas.create_text(bx + 8, by + 5 + lh * k + lh / 2, text=s,
                               anchor='w', font=f, fill='#1c1f26', tags=('tooltip',))

    def _show_detail(self):
        view = self._current_view()
        if 'detail' not in view:
            return
        sel = view['main'].selection()
        view['detail'].delete(*view['detail'].get_children())
        result = self._current_result()
        header = view.get('detail_header')
        if not sel or result is None:
            if header is not None:
                header.config(text='个股明细（点击上方日期行查看当日新高/新低个股）')
            return
        date = view['main'].item(sel[0], 'values')[0]
        day = next((d for d in result['days'] if d['date'] == date), None)
        if day is None:
            return
        for c in day['high']:
            view['detail'].insert('', 'end', values=(c['code'], c['name'], '创新高'),
                                  tags=('high',))
        for c in day['low']:
            view['detail'].insert('', 'end', values=(c['code'], c['name'], '创新低'),
                                  tags=('low',))
        if header is not None:
            header.config(text=f'{date}：创新高 {len(day["high"])} 只 · '
                               f'创新低 {len(day["low"])} 只')
        view['detail'].tag_configure('high', foreground=COLOR_HIGH)
        view['detail'].tag_configure('low', foreground=COLOR_LOW)
        self._stripe(view['detail'])
        self._reset_sort(view['detail'])

    # ---------- 操作 ----------
    def _show_block_names(self):
        """板块视图下方名称区：按当前选中日期行显示 OBV强势板块 与 OBV强势且60日线上方板块"""
        view = self._current_view()
        if 'strong' not in view:
            return
        view['strong'].delete(*view['strong'].get_children())
        view['above'].delete(*view['above'].get_children())
        result = self._current_result()
        if result is None:
            return
        sel = view['main'].selection()
        if not sel:
            return
        date = view['main'].item(sel[0], 'values')[0]
        for day in result['days']:
            if day['date'] != date:
                continue
            for n in day.get('obv_strong_names', []):
                view['strong'].insert('', 'end', values=(n,))
            for n in day.get('obv_above60_names', []):
                view['above'].insert('', 'end', values=(n,))
            break
        self._stripe(view['strong'])
        self._stripe(view['above'])

    def _on_refresh(self):
        # 只看当前数据源的加载状态（原全局互斥会被其他源的预加载卡住）
        if self._src_loading():
            return
        if self.cur_src == SRC_MARGIN:
            # 刷新两融：重查最新交易日 + 补齐缺失日
            self.margin_loading = True
            self.refresh_btn.config(state='disabled')
            self._set_status('正在刷新两融融资余额…')

            def worker4():
                try:
                    data = server.get_margin_daily(force=True,
                                                   progress_cb=self._margin_progress)
                    _log(f'refresh margin daily OK {data["meta"]["period"]}')
                    self._ui_call(lambda: self._refresh_margin_done(data))
                except Exception as e:
                    _log(f'refresh margin daily ERR {e}\n{traceback.format_exc()}')
                    self._ui_call(lambda msg=_err_text(e): self._refresh_fail(msg))

            threading.Thread(target=worker4, daemon=True).start()
        elif self.cur_src == SRC_BLOCK:
            # 刷新板块 OBV 逐日统计
            self.block_loading = True
            self.refresh_btn.config(state='disabled')
            self._set_status('正在刷新板块 OBV 统计（联网拉取最新行情）…')

            def worker():
                try:
                    data = server.get_block_obv_daily(force=True)
                    _log(f'refresh block obv daily OK {data["meta"]["period"]}')
                    self._ui_call(lambda: self._refresh_block_done(data))
                except Exception as e:
                    _log(f'refresh block obv daily ERR {e}\n{traceback.format_exc()}')
                    # 注意：except 变量 e 在 except 块结束后会被 Python 解除绑定，
                    # 若写成 lambda: ...(str(e))，回调执行时抛 NameError，失败提示永远不显示
                    self._ui_call(lambda msg=_err_text(e): self._refresh_fail(msg))

            threading.Thread(target=worker, daemon=True).start()
        elif self.cur_src == SRC_MARKET:
            # 刷新大盘情绪统计：force 只重查最近一个交易日，历史走缓存
            self.market_loading = True
            self.refresh_btn.config(state='disabled')
            self._set_status('正在刷新大盘情绪统计（重查最新交易日）…')

            def worker3():
                try:
                    data = server.get_market_daily(force=True)
                    _log(f'refresh market daily OK {data["meta"]["period"]}')
                    self._ui_call(lambda: self._refresh_market_done(data))
                except Exception as e:
                    _log(f'refresh market daily ERR {e}\n{traceback.format_exc()}')
                    # 注意：except 变量 e 在 except 块结束后会被 Python 解除绑定，
                    # 若写成 lambda: ...(str(e))，回调执行时抛 NameError，失败提示永远不显示
                    self._ui_call(lambda msg=_err_text(e): self._refresh_fail(msg))

            threading.Thread(target=worker3, daemon=True).start()
        else:
            # 刷新指数数据源（当前子选项卡）
            idx = self.cur_index
            name = INDEX_NAMES[idx]
            self.loading = True
            self.refresh_btn.config(state='disabled')
            self._set_status(f'正在刷新 {name}（联网拉取最新行情）…')

            def worker2():
                try:
                    data = server.compute(idx, force=True)
                    self.results[idx] = data
                    _log(f'refresh {name} OK {data["meta"]["period"]}')
                    self._ui_call(lambda: self._refresh_done(data))
                except Exception as e:
                    _log(f'refresh {name} ERR {e}\n{traceback.format_exc()}')
                    # 注意：except 变量 e 在 except 块结束后会被 Python 解除绑定，
                    # 若写成 lambda: ...(str(e))，回调执行时抛 NameError，失败提示永远不显示
                    self._ui_call(lambda msg=_err_text(e): self._refresh_fail(msg))

            threading.Thread(target=worker2, daemon=True).start()

    def _refresh_done(self, data):
        self.loading = False
        self.refresh_btn.config(state='normal')
        if self.cur_src == SRC_INDEX:
            self._apply_result(data)

    def _refresh_block_done(self, data):
        self.block_loading = False
        self.refresh_btn.config(state='normal')
        if self.cur_src == SRC_BLOCK:
            self.block_result = data
            self._apply_result(data)

    def _refresh_margin_done(self, data):
        self.margin_loading = False
        self.refresh_btn.config(state='normal')
        if self.cur_src == SRC_MARGIN:
            self.margin_result = data
            self._apply_result(data)

    def _refresh_market_done(self, data):
        self.market_loading = False
        self.refresh_btn.config(state='normal')
        if self.cur_src == SRC_MARKET:
            self.market_result = data
            self._apply_result(data)

    def _refresh_fail(self, err):
        self.loading = False
        self.block_loading = False
        self.market_loading = False
        self.margin_loading = False
        self.refresh_btn.config(state='normal')
        self._set_status(f'刷新失败：{err}')
        messagebox.showerror('刷新失败', f'联网刷新失败：\n{err}')

    def _on_export(self):
        result = self._current_result()
        if result is None:
            if self.cur_src == SRC_BLOCK:
                self._set_status('板块统计尚未就绪')
            elif self.cur_src == SRC_MARKET:
                self._set_status('大盘统计尚未就绪')
            elif self.cur_src == SRC_MARGIN:
                self._set_status('两融统计尚未就绪')
            return
        name = result['meta']['name']
        default = f'{name}_{result["meta"]["period"].replace(" ~ ", "_")}.xlsx'
        path = filedialog.asksaveasfilename(defaultextension='.xlsx', initialfile=default,
                                            filetypes=[('Excel', '*.xlsx')])
        if not path:
            return
        try:
            from openpyxl import Workbook
            wb = Workbook()
            if self.cur_src == SRC_MARKET:
                # 大盘视图：12列每日情绪统计
                ws = wb.active
                ws.title = '每日统计'
                ws.append([head for _, head, _, _ in MARKET_COLS])
                for day in result['days']:
                    row = []
                    for key, _, _, _ in MARKET_COLS:
                        v = day[key]
                        if key == 'vol' and v is not None:
                            v = round(v, 2)
                        row.append(v)
                    ws.append(row)
                wb.save(path)
                self._set_status(f'已导出：{path}')
                return
            if self.cur_src == SRC_MARGIN:
                # 两融视图：沪深京融资余额（亿元）
                ws = wb.active
                ws.title = '每日统计'
                ws.append(['日期', '沪市融资余额', '深市融资余额', '京市融资余额',
                           '合计', '较前一日'])
                for day in result['days']:
                    ws.append([day['date'], day['sh'], day['sz'], day['bj'],
                               day['total'], day['chg']])
                wb.save(path)
                self._set_status(f'已导出：{path}')
                return
            if self.cur_src == SRC_BLOCK:
                # 板块视图：三列每日统计
                ws = wb.active
                ws.title = '每日统计'
                ws.append(['日期', 'OBV强势板块数', 'OBV强势且60日线上方数'])
                for day in result['days']:
                    ws.append([day['date'], day['obv_strong'], day['obv_strong_above60']])
                wb.save(path)
                self._set_status(f'已导出：{path}')
                return
            # Sheet1：每日统计
            ws = wb.active
            ws.title = '每日统计'
            ws.append(['日期', '创新高数', '创新低数', '基差'])
            for day in result['days']:
                b = day.get('basis')
                ws.append([day['date'], len(day['high']), len(day['low']),
                           None if b is None else round(b, 2)])
            # Sheet2：个股明细（按天）
            ws2 = wb.create_sheet('个股明细')
            ws2.append(['日期', '代码', '名称', '类型'])
            for day in result['days']:
                for c in day['high']:
                    ws2.append([day['date'], c['code'], c['name'], '创新高'])
                for c in day['low']:
                    ws2.append([day['date'], c['code'], c['name'], '创新低'])
            wb.save(path)
            self._set_status(f'已导出：{path}')
        except Exception as e:
            messagebox.showerror('导出失败', str(e))

    def _set_status(self, msg):
        self.status_var.set(msg)

    def _on_close(self):
        """窗口关闭：保存窗口/数据源偏好，置位 _stop 停止 watchdog/预加载线程后销毁窗口"""
        self._stop = True
        try:
            geo = self.root.geometry()
            _m = re.match(r'^(\d+x\d+)([+-]\d+)([+-]\d+)$', geo)
            # 最小化状态下关闭时 geometry 为 -32000 类屏幕外坐标：
            # 保存它会让下次启动窗口跑到屏幕外，这里只存有效坐标
            if _m and int(_m.group(2)) > -3000 and int(_m.group(3)) > -3000:
                self.ui_cfg['geometry'] = geo
            self.ui_cfg['source'] = self.cur_src
            self.ui_cfg['index_tab'] = self.idx_notebook.index(self.idx_notebook.select())
            _save_ui_config(self.ui_cfg)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    # 单实例守卫：多开时激活已有窗口并退出，避免并发争抢 thsdk/SQLite
    # 导致个别实例预加载全部静默失败、界面永久空白（实测出现过）。
    if not _single_instance_guard():
        return
    _log(f'指数新高新低复盘启动 版本={server.APP_VERSION}')
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
