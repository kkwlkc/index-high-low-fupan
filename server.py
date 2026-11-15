# -*- coding: utf-8 -*-
"""A股指数成分股 收盘价创20日新高/新低统计 - 计算核心（桌面版）
支持指数：上证50(000016) / 沪深300(000300) / 中证2000(932000)
数据：同花顺thsdk日K线 + SQLite缓存
优化：prev20 计算向量化；缓存全表一次读取，避免逐只查询
"""
import contextlib
import bisect
import json
import os
import sys
import time
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TZ = ZoneInfo('Asia/Shanghai')

# 运行环境适配：exe 打包后资源在 _MEIPASS/_internal（只读），
# 数据文件（DB缓存/成分股csv）优先放 exe 同目录，便于用户替换更新
_FROZEN = getattr(sys, 'frozen', False)
if _FROZEN:
    EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, '_MEIPASS', EXE_DIR)
    DATA_DIR = EXE_DIR
    if os.path.exists(os.path.join(EXE_DIR, 'sz50_cons.csv')):
        CONS_DIR = EXE_DIR
    else:
        CONS_DIR = RES_DIR
else:
    RES_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = RES_DIR
    CONS_DIR = RES_DIR

DB = os.path.join(DATA_DIR, 'klines_idx.db')

# windowed 打包（无控制台）时 sys.stdout/sys.stderr 为 None，
# akshare 内部 tqdm 会向 stderr 写入而报 'NoneType' has no attribute 'write'，
# 导致板块数据拉取失败。这里防御性重定向到 exe 同目录日志文件。
if sys.stdout is None:
    try:
        sys.stdout = open(os.path.join(DATA_DIR, '复盘_stdout.log'), 'a', encoding='utf-8', buffering=1)
    except Exception:
        pass
if sys.stderr is None:
    try:
        sys.stderr = open(os.path.join(DATA_DIR, '复盘_stderr.log'), 'a', encoding='utf-8', buffering=1)
    except Exception:
        pass


def _dbg(msg):
    """调试日志：frozen 时写 exe 同目录 复盘_debug.log，源码运行写同目录"""
    try:
        with open(os.path.join(DATA_DIR, '复盘_debug.log'), 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n')
    except Exception:
        pass


# ---- thsdk 连接互斥（重要）----
# thsdk 通过 ctypes.CDLL 加载 hq.dll。Windows 的 LoadLibrary 对同一路径返回同一个
# 模块句柄，因此 dll 内部的连接状态是「进程级共享」的，而不是每个 THS() 实例一份。
# 后果：A 线程调用 disconnect()，会把 B 线程正在使用的连接一起断掉，B 的后续查询
# 报 "[thsdk]请先连接服务"（该字符串来自 dll 本身）。
# 表现：大盘问财回填与板块/指数拉取并发时，问财连续失败、当日数据缺失。
# 对策：所有 THS 会话统一走 ths_session()，全程串行化，杜绝交叉断连。
_THS_LOCK = threading.RLock()


@contextlib.contextmanager
def ths_session():
    """串行化的 thsdk 会话：进入时 connect，退出时 disconnect。

    全程持有 _THS_LOCK，保证同一时刻只有一个线程在使用底层 dll 连接。
    调用方不要自行创建 THS() 实例，否则会破坏串行化。
    """
    from thsdk import THS
    with _THS_LOCK:
        ths = THS()
        try:
            ths.connect()
            yield ths
        finally:
            try:
                ths.disconnect()
            except Exception:
                pass


INDEXES = {
    '000016': ('上证50', os.path.join(CONS_DIR, 'sz50_cons.csv')),
    '000300': ('沪深300', os.path.join(CONS_DIR, 'sz300_cons.csv')),
    '932000': ('中证2000', os.path.join(CONS_DIR, 'zz2000_cons.csv')),
}

# K线拉取起点（需早于统计起点至少20个交易日，作新高/新低基准）
START_BENCH = datetime(2026, 2, 20, tzinfo=TZ)
# 统计区间起点
TARGET_FROM = '2026-04-07'
# 大盘问财回填连续失败熔断阈值：THS/问财不可用时立即中止本轮，避免空转上百个日期
MARKET_MAX_CONSEC_FAIL = 3

# 每日基差：基差 = 现货收盘 - 期货全合约持仓量加权价
# 中证2000(932000)页展示中证1000(IM)的基差；000016->IH, 000300->IF
FUTURE_PREFIX = {'000016': 'IH', '000300': 'IF', '932000': 'IM'}
SPOT_MAP = {'000016': 'sh000016', '000300': 'sh000300', '932000': 'sh000852'}
_BASIS_CACHE = {}
_FUT_CONTRACT_CACHE = {}
# 基差"当日数据就绪"口径升级标记（用于一次性清理旧版落库的失真当日基差）
_BASIS_READY_FIX_KEY = 'basis_ready_fix_v1'

# 程序版本：修改代码后须重新打包，并以窗口标题/日志中的版本号核对实际运行程序，
# 避免"源码已修复、仍在跑旧 exe"的部署错位（曾让基差错误长时间未被修复）。
APP_VERSION = 'v1.5'


def to_ths(code):
    c = str(code).strip().zfill(6)
    if c.startswith(('60', '68', '51', '58', '56', '50')):
        return 'USHA' + c
    if c.startswith(('00', '30', '15', '16')):
        return 'USZA' + c
    if c.startswith(('8', '4', '92')):
        return 'USTM' + c
    return 'USHA' + c


# ---- 板块支持（同花顺行业板块） ----
_BLOCK_LIST_LOADED = False
_BLOCK_LIST_CACHE = []       # [(板块代码, 板块名称)]
_BLOCK_CONS_CACHE = {}       # {板块代码: [(ths代码, 名称)]} 内存缓存

# 板块今日行情补点的最小覆盖率：thsdk 增量补当日时，成功补到的板块数须 >= 该比例，
# 否则整体放弃补当日（避免少数板块补到当日、多数停在上一日导致的"假新鲜"与计数失真）
_BLOCK_TODAY_MIN_COVER = 0.9
# 板块全量重建节流：akshare 板块指数源端收盘后常滞后一日，若 thsdk 当日行情又不可用，
# 会反复触发全量重建（约1-3分钟/次）空转。非 force 时两次重建最小间隔 30 分钟。
_BLOCK_REBUILD_TS = 0.0
_BLOCK_REBUILD_MIN_SEC = 1800


def load_block_list(force=False):
    """获取同花顺行业板块列表，返回 [(板块代码, 板块名称)]，带内存缓存"""
    global _BLOCK_LIST_LOADED
    if _BLOCK_LIST_LOADED and not force:
        return _BLOCK_LIST_CACHE
    with ths_session() as ths:
        resp = ths.ths_industry()
        df = resp.df
    items = [(str(c), str(n)) for c, n in zip(df['代码'], df['名称'])]
    _BLOCK_LIST_CACHE[:] = items
    _BLOCK_LIST_LOADED = True
    return items


def load_block_constituents(block_code):
    """获取板块成分股，返回 [(ths代码, 名称)]（已是ths格式，无需再转换），带内存缓存"""
    if block_code in _BLOCK_CONS_CACHE:
        return _BLOCK_CONS_CACHE[block_code]
    with ths_session() as ths:
        resp = ths.block_constituents(block_code)
        df = resp.df
    items = [(str(c), str(n)) for c, n in zip(df['代码'], df['名称'])]
    _BLOCK_CONS_CACHE[block_code] = items
    return items


def block_display_name(block_code):
    """板块代码 -> 板块名称（列表未加载时触发一次加载）"""
    for c, n in _BLOCK_LIST_CACHE:
        if c == block_code:
            return n
    try:
        for c, n in load_block_list():
            if c == block_code:
                return n
    except Exception:
        pass
    return block_code


def load_cons(idx_code):
    if str(idx_code).startswith('URFI'):
        # 板块代码：成分股已是 ths 格式，无需 to_ths 转换
        return load_block_constituents(idx_code)
    df = pd.read_csv(INDEXES[idx_code][1], encoding='utf-8-sig')
    return [(to_ths(str(c)), str(n))
            for c, n in zip(df['成分券代码'], df['成分券名称'])]


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    # 数据文件策略：优先使用 exe 同目录已存在的 DB（保留用户刷新结果）；
    # 若不存在或内置 DB 更新（built_at 更大），则用内置 DB 覆盖，保证首次打开即有完整数据
    bundled = os.path.join(RES_DIR, 'klines_idx.db')
    need_copy = not os.path.exists(DB)
    if not need_copy and os.path.exists(bundled):
        try:
            need_copy = _db_built_at(bundled) > _db_built_at(DB)
        except Exception:
            need_copy = False
    if need_copy and os.path.exists(bundled):
        try:
            shutil.copyfile(bundled, DB)
            print(f'[data] 使用内置数据缓存（built_at={_db_built_at(bundled)}）', flush=True)
        except Exception as e:
            print(f'[data] copy bundled db failed: {e}', flush=True)
    con = sqlite3.connect(DB)
    # WAL 模式：指数/板块/大盘三个预加载线程并行读写不再互相阻塞（写不挡读）
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=5000')
    con.execute("CREATE TABLE IF NOT EXISTS klines("
                "code TEXT PRIMARY KEY, name TEXT, data TEXT, status TEXT, err TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS block_obv("
                "code TEXT PRIMARY KEY, name TEXT, strong INTEGER, obv REAL, obv_ma REAL, days INTEGER, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS block_kline("
                "code TEXT PRIMARY KEY, name TEXT, data TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS market_daily("
                "date TEXT PRIMARY KEY, valid INTEGER, red INTEGER, green INTEGER,"
                "zt INTEGER, yizi INTEGER, fzt INTEGER, dt INTEGER, zhaban INTEGER,"
                "lb INTEGER, lb2 INTEGER, lb3 INTEGER, lb3p INTEGER, high_lb INTEGER, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS basis_series("
                "code TEXT, date TEXT, basis REAL, ts REAL,"
                "n INTEGER, ref INTEGER, PRIMARY KEY(code,date))")
    con.execute("CREATE TABLE IF NOT EXISTS market_volume("
                "date TEXT PRIMARY KEY, vol REAL, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS margin_daily("
                "date TEXT PRIMARY KEY, sh REAL, sz REAL, bj REAL, total REAL, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    # 旧库迁移：给 basis_series 补上"就绪证据"列 n/ref（写当日基差行时记录
    # 参与合约数/期望合约数）。basis_needs_refresh 依此做质量校验：失真中间态
    # 即便已入库也会被识别并安排联网重算覆盖，实现持续自愈（不只靠一次性清理）。
    for _col in ('n', 'ref'):
        try:
            con.execute(f'ALTER TABLE basis_series ADD COLUMN {_col} INTEGER')
        except Exception:
            pass  # 已存在
    con.commit()
    con.close()
    # 一次性清理：旧口径落库的最新交易日基差可能失真（部分合约未出数据即加权），
    # 会被判定为"已覆盖 T"而永不重算，这里清掉后由自动更新重新按新口径补。
    try:
        _purge_basis_today_once()
    except Exception as e:
        _dbg(f'purge basis ERR {type(e).__name__}: {e}')


# ---- 交易日历 ----
# 原来 _latest_trading_date / _prev_trading_date 只跳过周末、不认节假日，
# 国庆/春节等长假期间会把休市日当成交易日，导致每天反复联网拉取不存在的数据。
# 这里统一以 akshare 交易日历为准，拿不到日历时回退到原有的"仅跳过周末"近似。
_CAL_LOCK = threading.Lock()
_CAL_DAYS = None      # 升序交易日列表 ['YYYY-MM-DD', ...]，含未来日期
_CAL_TRIED = False    # 是否已尝试过联网拉取（失败后本进程不再重试，避免反复拖慢启动）


def _calendar():
    """交易日列表（升序，含未来日期）。首次调用联网拉取并缓存，失败返回 None。"""
    global _CAL_DAYS, _CAL_TRIED
    if _CAL_DAYS is not None or _CAL_TRIED:
        return _CAL_DAYS
    with _CAL_LOCK:
        if _CAL_DAYS is None and not _CAL_TRIED:
            _CAL_TRIED = True
            try:
                import akshare as ak
                df = ak.tool_trade_date_hist_sina()
                days = sorted(str(r['trade_date'])[:10] for _, r in df.iterrows())
                if days:
                    _CAL_DAYS = days
            except Exception as e:
                _dbg(f'trade calendar ERR {type(e).__name__}: {e}')
    return _CAL_DAYS


def _latest_trading_date():
    """最近一个完整交易日：今天已过 15:10 且今天为交易日则取今天，否则取上一交易日。"""
    now = datetime.now(TZ)
    today = now.date().isoformat()
    after_close = now.hour * 60 + now.minute >= 15 * 60 + 10
    days = _calendar()
    if days:
        i = bisect.bisect_left(days, today)
        if after_close and i < len(days) and days[i] == today:
            return today
        return days[i - 1] if i > 0 else today
    # 回退：仅按周末近似（节假日无法识别）
    d = now.date()
    if after_close and d.weekday() < 5:
        return d.isoformat()
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d.isoformat()


def _prev_trading_date(t):
    """t 的上一交易日（按交易日历，正确跨越节假日）。"""
    days = _calendar()
    if days:
        i = bisect.bisect_left(days, t)
        if i > 0:
            return days[i - 1]
    d = datetime.strptime(t, '%Y-%m-%d').date() - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d.isoformat()


def cache_latest(rows):
    """最新级新鲜：缓存已包含最近一个完整交易日（T）"""
    if not rows:
        return False
    return rows[-1]['时间'] >= _latest_trading_date()


def cache_renderable(rows):
    """渲染级新鲜：K线至少到最近交易日前一日（T-1）即可直接渲染。
    B方案核心：成分停留在T-1也能秒出界面，最新T由后台增量静默补拉。"""
    if not rows:
        return False
    return rows[-1]['时间'] >= _prev_trading_date(_latest_trading_date())


def idx_cache_latest(idx_code, tol=0.005):
    """该指数成分股K线缓存是否已覆盖最近完整交易日T；
    允许极少数成分因停牌/数据缺失未覆盖（默认 ≤0.5%，至少1只），
    避免个别死穴股导致每日都全量重拉。True 时仅加载缓存零联网，否则需全量加载。"""
    latest = _latest_trading_date()
    raw = _load_raw_cache()
    cons = load_cons(idx_code)
    missing = [c for c, _ in cons
               if not (raw.get(c) and raw[c][-1]['时间'] >= latest)]
    tol_n = max(1, int(len(cons) * tol))
    return len(missing) <= tol_n


def block_cache_latest():
    """板块逐日OBV统计缓存是否已覆盖最近完整交易日T（比对 meta 实际写入日期）。"""
    latest = _latest_trading_date()
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    row = con.execute("SELECT value FROM meta WHERE key='block_obv_daily_date'").fetchone()
    con.close()
    return bool(row and row[0] == latest)


def market_cache_latest():
    """大盘情绪统计缓存是否已覆盖最近完整交易日T（含 valid=0 节假日探测记录）。"""
    latest = _latest_trading_date()
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    row = con.execute("SELECT 1 FROM market_daily WHERE date=?", (latest,)).fetchone()
    con.close()
    return row is not None


def _db_built_at(path):
    try:
        con = sqlite3.connect(path)
        row = con.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
        con.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


_RAW_CACHE = None  # 进程内全表缓存：{code: rows}（原始，未做新鲜度过滤）


def _load_raw_cache(force=False):
    """一次连接读全表 K线并反序列化，进程内缓存，供各处共享。

    原先 load_all_cache 与 idx_cache_latest 各自全表扫描，自动更新时三个指数
    连续调用就是 3 次全表读取 + 32MB JSON 解析。这里统一收口为一次。
    """
    global _RAW_CACHE
    if _RAW_CACHE is not None and not force:
        return _RAW_CACHE
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    rows = con.execute("SELECT code, data FROM klines").fetchall()
    con.close()
    out = {}
    for code, data in rows:
        try:
            d = json.loads(data)
        except Exception:
            continue
        if d:
            out[code] = d
    _RAW_CACHE = out
    return out


def load_all_cache(force=False):
    """返回 {code: rows}，仅含渲染级新鲜数据（K线 >= T-1）。

    基于 _RAW_CACHE 派生，不再单独缓存一份，避免两份缓存不一致。
    """
    raw = _load_raw_cache(force=force)
    return {c: r for c, r in raw.items() if cache_renderable(r)}


def _bulk_save_cache(items):
    """批量写 klines 缓存：一次连接 + 一次 commit（替代逐股开连接提交，
    中证2000 全量 2000 次连接+commit → 数批完成，DB 写入提速明显）。
    items: [(code, name, rows)]，rows 非 None。同时增量更新进程内全表缓存。"""
    global _RAW_CACHE
    if not items:
        return
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    con.executemany(
        "INSERT OR REPLACE INTO klines(code,name,data,status,err,ts) VALUES(?,?,?,?,?,?)",
        [(c, n, json.dumps(r, ensure_ascii=False), 'ok', None, time.time())
         for c, n, r in items])
    con.commit()
    con.close()
    if _RAW_CACHE is not None:
        for c, n, r in items:
            if r:
                _RAW_CACHE[c] = r


def _bulk_merge_cache(items):
    """批量把新拉K线增量合入已有缓存（按时间去重），一次连接 + 一次 commit。
    items: [(code, name, new_rows)]。同时增量更新进程内全表缓存。"""
    global _RAW_CACHE
    if not items:
        return
    merged_out = {}
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    for code, name, new_rows in items:
        row = con.execute("SELECT data FROM klines WHERE code=?", (code,)).fetchone()
        try:
            old = json.loads(row[0]) if row else None
        except Exception:
            old = None
        if old:
            seen = {r['时间'] for r in old}
            merged = old + [r for r in new_rows if r['时间'] not in seen]
            merged.sort(key=lambda r: r['时间'])
        else:
            merged = new_rows
        merged_out[code] = (name, merged)
    con.executemany(
        "INSERT OR REPLACE INTO klines(code,name,data,status,err,ts) VALUES(?,?,?,?,?,?)",
        [(c, n, json.dumps(r, ensure_ascii=False), 'ok', None, time.time())
         for c, (n, r) in merged_out.items()])
    con.commit()
    con.close()
    if _RAW_CACHE is not None:
        for c, (n, r) in merged_out.items():
            if r:
                _RAW_CACHE[c] = r


def pull_chunk(chunk, workers=1, gap=0.02, count=-1):
    """串行拉取一批并复用单个 THS 长连接。

    thsdk 官方对 klines 限频 20ms/次（服务端实际还有账户级风控），
    实测多连接并发会撞限频/被断连，反而更慢；
    单连接复用则每只仅一次握手，2000 只全量约 2~3 分钟。
    连续失败超过阈值时重建连接再继续。返回 {code: rows|None}。
    count>0 仅作预留（thsdk 限制 count 不能与时间区间同用，且游客账号
    下此路径不稳定，实战一律走全量）。"""
    from thsdk import THS
    results = {}
    n = len(chunk)
    _dbg(f'pull_chunk start chunk={n} count={count}')

    def _fetch(ths, code, name):
        end = datetime.now(TZ) + timedelta(days=1)
        if count > 0:
            resp = ths.klines(code, interval='day', count=count)
        else:
            resp = ths.klines(code, interval='day',
                              start_time=START_BENCH, end_time=end)
        if resp and resp.df is not None and not resp.df.empty:
            df = resp.df.reset_index()
            df['时间'] = pd.to_datetime(df['时间']).dt.strftime('%Y-%m-%d')
            return df[['时间', '收盘价', '最高价', '最低价']].to_dict('records')
        return None  # 返回空视为未取到，交由外层重试

    steps = chunk[:]
    i = 0
    consec_fail = 0
    ths = None
    # 全程持有连接锁：本函数自行管理 connect/disconnect（连续失败时重建连接），
    # 期间不允许其他线程触碰同一个 dll 连接，否则会被对方的 disconnect 掐断。
    with _THS_LOCK:
        try:
            while i < n:
                # 连接不存在或连续失败过多时重建
                if ths is None or consec_fail >= 5:
                    if ths is not None:
                        try:
                            ths.disconnect()
                        except Exception:
                            pass
                        ths = None
                    ths = THS()
                    try:
                        ths.connect()
                    except Exception as e:
                        _dbg(f'pull_chunk reconnect ERR {type(e).__name__}: {e}')
                        ths = None
                        time.sleep(1.0)
                        continue
                    consec_fail = 0
                code, name = steps[i]
                ok = False
                for attempt in range(3):
                    try:
                        rows = _fetch(ths, code, name)
                        if rows is not None:
                            results[code] = rows
                            ok = True
                            break
                    except Exception as e:
                        _dbg(f'klines {code}({name}) attempt{attempt} ERR {type(e).__name__}: {e}')
                        time.sleep(0.4 * (attempt + 1))
                if ok:
                    consec_fail = 0
                else:
                    consec_fail += 1
                    _dbg(f'klines {code}({name}) FAILED (consec={consec_fail})')
                i += 1
                time.sleep(gap)
        finally:
            if ths is not None:
                try:
                    ths.disconnect()
                except Exception:
                    pass
    _dbg(f'pull_chunk done ok={len(results)}/{n}')
    return results


def ensure_fresh(cons, progress_cb=None, force=False):
    """确保每只成分股K线最新：非force先全表读缓存，缺失/不新鲜才联网拉取；
    force=True 时全部联网重拉。返回 {code: rows}"""
    out = {}
    todo = []
    if force:
        todo = list(cons)
    else:
        cache = load_all_cache()
        for code, name in cons:
            if code in cache:
                out[code] = cache[code]
            else:
                todo.append((code, name))

    if not todo:
        return out

    # 分片拉取，失败块换连接重试一次
    CHUNK = 200
    done_base = len(cons) - len(todo)
    cons_map = dict(cons)
    with _NET_LOCK:  # 与后台增量刷新互斥，避免并发争用 thsdk
        for i in range(0, len(todo), CHUNK):
            chunk = todo[i:i + CHUNK]
            for attempt in range(2):
                res = pull_chunk(chunk)
                # pull_chunk 只把成功的股票放进 results，失败的根本不在字典里。
                # 因此失败列表必须用「本批代码 - 成功代码」求差集得出；
                # 原写法 [c for c, r in res.items() if r is None] 恒为空，
                # 导致重试分支永不触发、失败个股被静默丢弃。
                failed = {c for c, _ in chunk if c not in res}
                items = []
                for code, rows in res.items():
                    old = out.get(code)
                    if old:
                        merged = {r['时间']: r for r in old}
                        merged.update({r['时间']: r for r in rows})
                        rows = [merged[d] for d in sorted(merged)]
                    items.append((code, cons_map[code], rows))
                    out[code] = rows
                if items:
                    _bulk_save_cache(items)  # 批量写库（一次连接+commit），提速显著
                if not failed:
                    break
                _dbg(f'ensure_fresh retry {len(failed)} failed of chunk '
                     f'[{i}:{i + CHUNK}] (attempt {attempt + 1})')
                chunk = [c for c in chunk if c[0] in failed]
            if failed:
                # 两轮仍失败：记录具体代码，便于排查停牌/退市/数据源问题
                _dbg(f'ensure_fresh give up {len(failed)} codes: '
                     f'{sorted(failed)[:10]}{"..." if len(failed) > 10 else ""}')
            if progress_cb:
                # 进度分母应为全部成分数 len(cons)，而非本次待拉数 len(todo)，
                # 否则缓存命中越多进度越虚高、甚至超过 100%
                progress_cb(done_base + min(i + CHUNK, len(todo)), len(cons))
    return out


# ---- B方案：最新交易日后台增量刷新 ----
# 渲染级新鲜（到T-1）即先渲染，最新交易日T由后台线程静默补拉，不阻塞界面。
_NET_LOCK = threading.Lock()        # 全局网络互斥：预加载/手动刷新与后台刷新串行
_BG_LOCK = threading.Lock()         # 后台任务队列锁
_BG_ACTIVE = False                  # 是否有后台刷新线程在跑
_BG_GEN = 0                         # 后台 worker 代际号（用于竞态时避免错误复位 _BG_ACTIVE）
_BG_PENDING = set()                 # 待补拉的 (idx_code, code, name)
_BG_CBS = {}                        # idx_code -> on_latest(data) 回调（GUI侧）
_TL = threading.local()             # 线程本地状态：见 _in_bg 说明
# 后台线程内 compute 递归标志。
# 必须是线程本地的：原实现用全局 bool，_bg_worker 在线程内把它置 True 后
# 会泄漏到其他线程——三指数并行预加载时，先提交的指数启动后台线程置位后，
# 其余指数的 compute 会误判自己"在后台线程内"而跳过补拉提交，
# 导致中证2000 等指数的成分股永远停在 T-1（库里长期半补状态的根因）。
def _in_bg_get():
    return getattr(_TL, 'in_bg', False)


def _bg_submit(idx_code, codes, cons_map, on_latest):
    """提交一批需要补到最新交易日T的股票；若已有后台线程在跑则并入队列。
    返回 True 表示本进程新启动了后台线程。"""
    global _BG_ACTIVE, _BG_GEN
    started = False
    gen = None
    with _BG_LOCK:
        _BG_CBS[idx_code] = on_latest
        # 预先取出已排队的 code 集合：原写法在循环内重复构造
        # {x[1] for x in _BG_PENDING}，中证2000 下是 O(n²)，这里降为 O(n)
        queued = {x[1] for x in _BG_PENDING}
        for c in codes:
            if c not in queued:
                _BG_PENDING.add((idx_code, c, cons_map.get(c, c)))
                queued.add(c)
        if not _BG_ACTIVE:
            _BG_ACTIVE = True
            _BG_GEN += 1
            gen = _BG_GEN
            started = True
    if started:
        threading.Thread(target=_bg_worker, args=(gen,), daemon=True).start()
    return started


def _bg_worker(gen, depth=0):
    """后台线程：把队列中所有停在T-1的成分增量补拉到最新T，随后对受影响的
    每个指数重算结果并通过 on_latest 回调通知界面刷新。

    depth：续跑轮次。重算期间若又产生新的待办，本 worker 会在 finally 里
    起一个同代 worker 继续处理（见 finally 注释），depth 用于限制续跑次数。"""
    global _BG_ACTIVE, _BG_PENDING, _BG_GEN
    try:
        while True:
            with _BG_LOCK:
                if not _BG_PENDING:
                    break
                batch = list(_BG_PENDING)
                _BG_PENDING = set()
            pairs = [(c, n) for (_i, c, n) in batch]
            # 预建 code->name 映射：原写法对每个结果线性扫描 batch（O(n²)）
            name_of = {c: n for (_i, c, n) in batch}
            _dbg(f'[bg] incremental refresh start n={len(pairs)}')
            CHUNK = 200
            with _NET_LOCK:
                for i in range(0, len(pairs), CHUNK):
                    chunk = pairs[i:i + CHUNK]
                    for attempt in range(2):
                        res = pull_chunk(chunk)
                        # 同 ensure_fresh：失败代码不在 res 里，必须用差集求
                        failed = {c for c, _ in chunk if c not in res}
                        items = []
                        for code, rows in res.items():
                            items.append((code, name_of.get(code, code), rows))
                        if items:
                            _bulk_merge_cache(items)  # 批量合并去重写库（一次连接+commit）
                        if not failed:
                            break
                        _dbg(f'[bg] retry {len(failed)} failed of chunk '
                             f'[{i}:{i + CHUNK}] (attempt {attempt + 1})')
                        chunk = [(c, n) for (c, n) in chunk if c in failed]
                    if failed:
                        _dbg(f'[bg] give up {len(failed)} codes')
            _dbg(f'[bg] incremental refresh done n={len(pairs)}')
        # 全部补拉完成后，重算受影响指数的完整结果并回调界面。
        # 预加载多指数并发 compute 时回调可能稍后注册，循环取增量直至队列清空，
        # 确保每个提交过 on_latest 的指数（含仅基差缺T的情况）都被重算到。
        # 处理完即从 _BG_CBS 移除；同指数后续再次提交（新回调对象）会在下轮处理。
        # 退出时在锁内原子复位 _BG_ACTIVE：若新回调恰在旧 worker 收尾与复位之间
        # 注册，旧 worker 曾因「seen-empty 提前退出」而不复位，新回调被永久遗漏
        # （对应基差长期显示'-'的竞态）。
        while True:
            with _BG_LOCK:
                if not _BG_CBS:
                    break
                cbs = dict(_BG_CBS)
            for idx, cb in list(cbs.items()):
                if cb is None:
                    with _BG_LOCK:
                        _BG_CBS.pop(idx, None)
                    continue
                _TL.in_bg = True   # 仅本线程可见，避免污染其他预加载线程
                try:
                    # 后台重算：强制补齐最新交易日 T 的基差（渲染级容忍后启动不再联网拉基差）
                    _dbg(f'[bg] recompute {idx} start refresh_basis=True')
                    data = compute(idx, refresh_basis=True)
                    _dbg(f'[bg] recompute {idx} done')
                except Exception as e:
                    _dbg(f'[bg] recompute {idx} ERR {e}')
                    data = None
                finally:
                    _TL.in_bg = False
                if data is not None:
                    try:
                        cb(data)
                    except Exception as e:
                        _dbg(f'[bg] callback ERR {e}')
                # 处理完移除；若期间被重新提交（新回调对象），保留新回调下次处理
                with _BG_LOCK:
                    if _BG_CBS.get(idx) is cb:
                        _BG_CBS.pop(idx, None)
    finally:
        # 仅当仍是本代 worker 时才复位 _BG_ACTIVE：若期间新 worker 已被启动
        # （_BG_GEN 已递增），旧 worker 不能误把新 worker 的标志复位，
        # 否则会出现两个后台线程并发、重复补拉（原实现无条件复位即存在此竞态）。
        with _BG_LOCK:
            if _BG_GEN == gen:
                # 重算阶段 compute 内部可能又提交了补拉（_bg_submit 见 _BG_ACTIVE
                # 为 True 时只入队、不新起线程）。这些残留条目若随本次退出一起复位
                # _BG_ACTIVE，就再没有 worker 会处理它们（实测出现过 _BG_PENDING=2
                # 且 _BG_ACTIVE=False 的僵局，2 只个股永远停在 T-1）。
                # 这里保留本代标志并交给新 worker 续跑；depth 上限防止意外死循环。
                if depth < 4 and (_BG_PENDING or _BG_CBS):
                    _dbg(f'[bg] leftover pending={len(_BG_PENDING)} cbs={len(_BG_CBS)}, '
                         f'restart worker depth={depth + 1}')
                    threading.Thread(target=_bg_worker, args=(gen, depth + 1),
                                     daemon=True).start()
                else:
                    _BG_ACTIVE = False


# 新浪期货行情全局并发上限：三指数并行 × 每指数 8 线程会瞬时发出 24 个请求，
# 触发限频时个别合约会静默失败并被缓存为 None，导致加权价失真（基差算错）。
# 用信号量把全局在途请求压到 8，实测该并发度下结果与串行完全一致。
_FUT_FETCH_SEM = threading.Semaphore(8)


def _fut_contract_hist(pref, code, refresh_to=None, active_since=None):
    """拉取单个期货合约历史日线（含已到期合约），带进程内缓存。失败返回 None。

    refresh_to / active_since：缓存突破参数。新浪当日行情是「分批延迟发布」的，
    若某合约在"当日数据尚未发布"时被拉取并永久缓存，之后所有强制刷新都只会
    拿到这份不含最新交易日的旧数据——实测中证2000(IM) 连续 11 次重算都停在
    T-1，界面基差永远显示'-'（force=True 也无效，因为压根没再联网）。
    约定：仅当缓存末条日期落在 [active_since, refresh_to) 区间（合约仍在交易、
    但当日数据未出）时才重新联网；已含当日数据、或已到期（末条 < active_since）、
    或从未取到数据（未上市合约）的条目直接复用缓存，避免对大量无效合约重复空发请求。
    """
    if code in _FUT_CONTRACT_CACHE:
        cached = _FUT_CONTRACT_CACHE[code]
        if not refresh_to or cached is None:
            return cached
        try:
            last = str(cached['date'].iloc[-1])[:10]
        except Exception:
            last = ''
        if last >= refresh_to:
            return cached                      # 已含当日数据
        if not last or (active_since and last < active_since):
            return cached                      # 已到期/空数据：不再重复联网
    try:
        import akshare as ak
        with _FUT_FETCH_SEM:
            df = ak.futures_zh_daily_sina(symbol=code)
        _FUT_CONTRACT_CACHE[code] = df
        return df
    except Exception as e:
        # 尚未上市/已到期合约拉不到数据是正常情况（如候选枚举出的 IH2611），
        # 只在调试日志留痕，不再打到 stdout 日志里刷屏
        _dbg(f'[basis] contract {code} ERR {type(e).__name__}: {e}')
        _FUT_CONTRACT_CACHE[code] = None
        return None


def _fut_contract_batch(pref, codes, refresh_to=None, active_since=None):
    """并发拉取一批期货合约日线，返回 [(code, df|None), ...]（保持输入顺序）。
    并发度控制在 8，避免触发新浪行情接口限频。"""
    if not codes:
        return []
    from concurrent.futures import ThreadPoolExecutor
    workers = min(8, len(codes))
    if workers <= 1:
        return [(c, _fut_contract_hist(pref, c, refresh_to, active_since)) for c in codes]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        dfs = list(ex.map(lambda c: _fut_contract_hist(pref, c, refresh_to, active_since),
                          codes))
    return list(zip(codes, dfs))


def _fut_weighted_daily(pref, start, end, refresh_to=None, active_since=None):
    """返回 (加权价map, 参与合约数map)：统计区间内所有上市合约按日持仓量加权。
    候选合约：当月/下月/季月（股指期货只挂 当月、下月、随后2个季月），
    提前上市等特殊情况通过逐个尝试拉取自动包含。
    参与合约数用于校验最新交易日数据是否就绪（收盘后远月合约当日数据可能延迟）。"""
    from datetime import datetime as _dt
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    # 多枚举4个月，覆盖交割日后新挂出的"下月"合约（如8-21交割后新挂2610）
    # 及后续远月/季月合约，确保最新交易日与远期合约全部进入候选枚举。
    for _ in range(4):
        ey, em = (ey + 1, 1) if em == 12 else (ey, em + 1)
    cands = set()
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yymm = f'{y % 100:02d}{m:02d}'
        cands.add(f'{pref}{yymm}')
        # 下月
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        cands.add(f'{pref}{(ny % 100):02d}{nm:02d}')
        # 随后两个季月（3、6、9、12月）
        q = ((m - 1) // 3 + 1) * 3
        cnt = 0
        yy, mm = y, q
        while cnt < 2:
            if mm > m or yy > y:  # 只取未来季月
                cands.add(f'{pref}{yy % 100:02d}{mm:02d}')
                cnt += 1
            yy, mm = (yy + 1, 3) if mm == 12 else (yy, mm + 3)
        y, m = ny, nm
    out = {}
    ncnt = {}
    # 并发拉取候选合约：原实现串行逐个请求新浪（每个约 1.3s，13+ 个合约 ≈ 30-45s），
    # 三个指数串行就要约 2 分钟，自动更新/后台 worker 一旦被用户关闭程序打断，
    # 后面的指数基差就永远停在旧交易日（实测只有部分指数补到 T）。
    # akshare 此处是纯 HTTP 请求、无连接互斥（不同于 thsdk 的进程级连接），可安全并发。
    codes = sorted(cands)
    dfs = _fut_contract_batch(pref, codes, refresh_to, active_since)
    for code, df in dfs:
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            d = str(r['date'])[:10]
            if d < start or d > end:
                continue
            close = r['close']
            hold = r['hold']
            if pd.notna(close) and pd.notna(hold) and hold > 0:
                p, h = out.get(d, (0.0, 0.0))
                out[d] = (p + float(close) * float(hold), h + float(hold))
                ncnt[d] = ncnt.get(d, 0) + 1
    return ({d: p / h for d, (p, h) in out.items() if h > 0}, ncnt)


def _index_daily_last_date(df):
    """取指数日线 DataFrame 的最大日期（'YYYY-MM-DD'），异常返回 ''。"""
    try:
        d = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        return max(d)
    except Exception:
        return ''


def _spot_index_daily(spot, latest):
    """取现货指数日线，返回 (DataFrame|None, 数据源)。

    腾讯源收盘后偶发滞后：它返回非空、不抛异常，但最新交易日缺失
    （实测 sh000300 在 2026-09-08 收盘后只到 09-07，而同期 sh000852 正常到 09-08）。
    原实现只在"抛异常/返回空"时才回退新浪源，于是拿着滞后数据却当作成功，
    当日基差直接缺失——这正是沪深300 基差长期显示'-'的根因。
    这里在腾讯源未覆盖 latest 时主动再取新浪源，并取日期更新的那一份。
    """
    import akshare as ak
    best, src = None, None
    try:
        s = ak.stock_zh_index_daily_tx(symbol=spot)
        if s is not None and len(s) > 0:
            best, src = s, 'tx'
    except Exception as e:
        _dbg(f'[basis] spot tx {spot} ERR {type(e).__name__}: {e}')
    if best is None or _index_daily_last_date(best) < latest:
        try:
            s2 = ak.stock_zh_index_daily(symbol=spot)
            if s2 is not None and len(s2) > 0:
                if best is None or (_index_daily_last_date(s2)
                                    > _index_daily_last_date(best)):
                    best, src = s2, 'sina'
        except Exception as e:
            _dbg(f'[basis] spot sina {spot} ERR {type(e).__name__}: {e}')
    if best is not None:
        _dbg(f'[basis] spot {spot} src={src} last={_index_daily_last_date(best)} '
             f'need={latest}')
    return best, src


def _delete_basis_days(idx_code, days):
    """删除指定日期的基差缓存行。

    用于"当日数据未就绪"时清除可能已入库的失真值：basis_needs_refresh 只看
    MAX(date)，失真值一旦入库就会被判定为"已覆盖 T"而停止重试，界面将长期
    显示错误基差（2026-09-09 上证50 28.74 / 沪深300 55.2 即由此产生）。
    """
    if not days:
        return
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        con.executemany("DELETE FROM basis_series WHERE code=? AND date=?",
                        [(str(idx_code), d) for d in days])
        con.commit()
        con.close()
        try:
            _BASIS_CACHE.pop(str(idx_code), None)
        except Exception:
            pass
    except Exception as e:
        _dbg(f'delete basis days ERR {type(e).__name__}: {e}')


def _purge_basis_today_once():
    """一次性清理：删除最近交易日的基差缓存行，交由新口径重算。

    旧版本在"当日只有部分合约出数据"时也会落库（加权价缺远月合约 → 基差虚低），
    这些失真值会被 basis_needs_refresh 判定为"已覆盖 T"从而永不重试。
    这里在升级后首次启动时清掉当日行，之后由自动更新按"合约数齐备"的新口径
    重算并在未就绪时保持缺失、继续重试。
    """
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        row = con.execute("SELECT value FROM meta WHERE key=?",
                          (_BASIS_READY_FIX_KEY,)).fetchone()
        if row and row[0] == '1':
            con.close()
            return
        latest = _latest_trading_date()
        cur = con.execute("DELETE FROM basis_series WHERE date>=?", (latest,))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    (_BASIS_READY_FIX_KEY, '1'))
        con.commit()
        con.close()
        if cur.rowcount:
            print(f'[data] 清理失真基差缓存 {cur.rowcount} 行（date>={latest}）', flush=True)
    except Exception as e:
        _dbg(f'purge basis today ERR {type(e).__name__}: {e}')


def get_basis_series(idx_code, force=False):
    """每日基差序列 {date: basis}，基差 = 现货收盘 - 期货全合约持仓量加权价。
    中证2000(932000)页返回中证1000(IM)的基差。
    缓存：进程内 dict + SQLite basis_series 表双层；缓存已覆盖最近一个完整
    交易日时免联网直接返回（原来每次启动都要联网拉现货+全部期货合约）。"""
    idx_code = str(idx_code)
    if idx_code in _BASIS_CACHE and not force:
        return _BASIS_CACHE[idx_code]
    pref = FUTURE_PREFIX.get(idx_code)
    spot = SPOT_MAP.get(idx_code)
    if not pref or not spot:
        _BASIS_CACHE[idx_code] = {}
        return _BASIS_CACHE[idx_code]
    # SQLite 快速路径：缓存已含最近交易日则免联网（启动提速）
    if not force:
        try:
            con = sqlite3.connect(DB)
            con.execute('PRAGMA busy_timeout=5000')
            rows = con.execute("SELECT date, basis FROM basis_series WHERE code=?",
                               (idx_code,)).fetchall()
            # 渲染级容忍：缓存覆盖到 T-1 即免联网（启动提速），最新 T 由后台补齐
            if rows and max(d for d, _ in rows) >= _prev_trading_date(_latest_trading_date()):
                out = {d: b for d, b in rows}
                # 渲染层不展示失真当日值：T 行若缺"合约齐备"就绪证据(n/ref)则
                # 暂显'-'，由自动更新联网重算覆盖后再经 on_latest 回调刷新界面
                # （宁可短暂 '-',也不显示错误数值）。
                try:
                    _latest = _latest_trading_date()
                    if _latest in out:
                        rq = con.execute(
                            "SELECT n, ref FROM basis_series WHERE code=? AND date=?",
                            (idx_code, _latest)).fetchone()
                        if rq is None or rq[0] is None or rq[1] is None or \
                                (rq[1] > 0 and (rq[0] or 0) < rq[1]):
                            out.pop(_latest, None)
                except Exception:
                    pass
                con.close()
                _BASIS_CACHE[idx_code] = out
                return out
            con.close()
        except Exception as e:
            _dbg(f'basis cache read ERR {type(e).__name__}: {e}')
    try:
        import akshare as ak
        start = TARGET_FROM
        # 检索窗口向后扩展45天（跨自然月/季度），确保最新交易日及后续新上市/
        # 远月合约（当月/下月/随后两季月）全部进入候选枚举，提前检索基差数据。
        end = (datetime.now(TZ) + timedelta(days=45)).strftime('%Y-%m-%d')
        latest = _latest_trading_date()
        # 腾讯源优先，未覆盖最新交易日时自动回退新浪源（详见 _spot_index_daily）
        s, spot_src = _spot_index_daily(spot, latest)
        if s is None or len(s) == 0:
            raise RuntimeError(f'spot {spot} both sources empty')
        # 关键：把 latest/prev_td 传给合约拉取层，突破"数据未发布时已缓存"的
        # 旧快照。否则当日数据延迟时，进程内缓存会让后续 force 刷新全部空转
        # （中证2000 基差永远停在 T-1 的直接原因）。
        prev_td = _prev_trading_date(latest)
        fmap, fcnt = _fut_weighted_daily(pref, start, end,
                                         refresh_to=latest, active_since=prev_td)
        out = {}
        n_latest = ref_n = 0    # 就绪证据：最新交易日参与合约数 / 期望合约数
        for _, r in s.iterrows():
            d = str(r['date'])[:10]
            if d in fmap:
                out[d] = round(float(r['close']) - fmap[d], 2)
        # 最新交易日就绪判定（基差数值正确性的关键）：
        # 新浪当日行情按合约分批延迟发布，若只有部分合约出齐就做持仓量加权，
        # 加权价会明显偏离——远月合约贴水更深，缺失会抬高加权价、使基差虚低。
        # 实测 2026-09-09 16:38：IH 仅 3/4 合约有当日数据 → 基差 28.74，
        # 全部到齐后应为 46.23；IF 同样虚低（55.2，全齐 71.94）。
        # 因此以"最近若干交易日的正常合约数"为基准判定就绪：未就绪则剔除当日、
        # 不落库并删除可能已存在的失真行；就绪则把当日行的 n/ref 一并入库，
        # 供 basis_needs_refresh 做质量关判定（失真中间态即便入库也会被重算覆盖）。
        try:
            hist = [fcnt[d] for d in sorted(fcnt) if d < latest]
            ref_n = max(hist[-10:]) if hist else 0
            n_latest = fcnt.get(latest, 0)
            if n_latest >= max(1, ref_n):
                _dbg(f'basis {idx_code} {latest} ready contracts={n_latest}/{ref_n}')
            else:
                _dbg(f'basis {idx_code} {latest} 期货当日数据未就绪：'
                     f'{n_latest}/{ref_n} 个合约，剔除当日不落库')
                out.pop(latest, None)
                _delete_basis_days(idx_code, [latest])
        except Exception:
            pass
        _BASIS_CACHE[idx_code] = out
        # 写入 SQLite 缓存供下次启动秒出
        try:
            con = sqlite3.connect(DB)
            con.execute('PRAGMA busy_timeout=5000')
            # T 行额外写入"就绪证据"n/ref（历史行留空），供 basis_needs_refresh
            # 质量关判定：失真中间态即便落库，下轮自动更新也会联网重算覆盖。
            _w_n = n_latest if latest in out else None
            _w_ref = ref_n if latest in out else None
            con.executemany(
                "INSERT OR REPLACE INTO basis_series(code,date,basis,ts,n,ref) "
                "VALUES(?,?,?,?,?,?)",
                [(idx_code, d, b, time.time(),
                  _w_n if d == latest else None,
                  _w_ref if d == latest else None)
                 for d, b in out.items()])
            con.commit()
            con.close()
        except Exception as e:
            _dbg(f'basis cache write ERR {type(e).__name__}: {e}')
        try:
            with open(os.path.join(DATA_DIR, 'basis_debug.log'), 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")}] '
                        f'get_basis_series {idx_code} OK days={len(out)} spot={spot_src} '
                        f'last={max(out) if out else "-"}:{out.get(max(out)) if out else "-"}\n')
        except Exception:
            pass
        return out
    except Exception as e:
        import traceback
        try:
            with open(os.path.join(DATA_DIR, 'basis_debug.log'), 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")}] get_basis_series '
                        f'{idx_code} ERR {e}\n{traceback.format_exc()}\n')
        except Exception:
            pass
        _BASIS_CACHE[idx_code] = {}
        return _BASIS_CACHE[idx_code]


_BASIS_TRY_TS = {}     # idx_code -> 上次强制补齐的 monotonic 时间（失败节流）


def basis_needs_refresh(idx_code):
    """基差缓存是否需要联网补齐（轻量查询，不联网）。

    判定两关：
    1) 覆盖关：MAX(date) 是否 >= 最新完整交易日 T（失真行会把 MAX 钉在 T，
       此关通过不代表当日数据正确）；
    2) 质量关：T 行是否携带"合约齐备"就绪证据（n>=ref>0）。旧口径在当日
       只有部分合约出数据时落库的失真值、以及缺 n/ref 的旧库行，都会在此
       被识别为"待刷新"，交由自动更新联网重算覆盖——持续自愈，不再依赖
       一次性 meta 清理。
    """
    idx_code = str(idx_code)
    try:
        latest = _latest_trading_date()
    except Exception:
        return False
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        row = con.execute(
            "SELECT MAX(date) FROM basis_series WHERE code=?", (idx_code,)).fetchone()
        if not (row and row[0] and row[0] >= latest):
            con.close()
            return True          # 覆盖关未过：最新完整交易日尚无基差
        q = con.execute(
            "SELECT n, ref FROM basis_series WHERE code=? AND date=?",
            (idx_code, latest)).fetchone()
        con.close()
        if q is None:
            return True          # MAX>=T 但 T 行缺失（异常态）：确保重算
        n, ref = q
        if n is None or ref is None:
            return True          # 旧库行无就绪证据：重算一次补齐
        if ref > 0 and (n or 0) < ref:
            return True          # 质量关未过：失真中间态，需联网重算覆盖
        return False
    except Exception as e:
        _dbg(f'basis_needs_refresh {idx_code} ERR {type(e).__name__}: {e}')
        return False


def ensure_basis_fresh(idx_code, min_interval=600):
    """若基差缓存未覆盖最新完整交易日 T，则强制联网补齐一次。

    返回 True 表示本次确实补到了 T。调用方（每日自动更新）应在 compute 之前
    逐个指数调用，不依赖后台 worker 重算循环的存活时间：worker 重算三个指数是
    串行的（每个需拉 20+ 个期货合约，约 25 秒），用户中途关闭程序时后面的指数
    基差就永远停在旧交易日（曾出现只有中证2000 到 T、上证50/沪深300 停在 T-1）。

    min_interval：同一指数强制补齐的最小间隔（秒）。收盘后远月合约当日数据可能
    延迟未就绪，此时补齐会失败；watchdog 每 2 分钟复查一次，若不限流会反复
    联网拉 20+ 个合约，因此失败/成功后都按该间隔节流。
    当日数据未就绪（"缺合约"）时缩短为 5 分钟后即可再试：新浪是分批延迟发布的，
    间隔太长会让当日基差迟迟出不来（默认由 20 分钟下调为 10 分钟，
    未就绪时进一步缩短到 5 分钟）。"""
    idx_code = str(idx_code)
    if not basis_needs_refresh(idx_code):
        return False
    now = time.monotonic()
    if now - _BASIS_TRY_TS.get(idx_code, 0.0) < min_interval:
        return False
    _BASIS_TRY_TS[idx_code] = now
    try:
        s = get_basis_series(idx_code, force=True)
    except Exception as e:
        _dbg(f'ensure_basis_fresh {idx_code} ERR {type(e).__name__}: {e}')
        return False
    latest = None
    try:
        latest = _latest_trading_date()
    except Exception:
        pass
    ok = bool(s and latest and latest in s)
    if not ok and min_interval > 300:
        # 未就绪（当日合约数据未出齐）：5 分钟后再试，而不是等满 min_interval
        _BASIS_TRY_TS[idx_code] = now - (min_interval - 300)
    return ok


def refresh_all_basis(codes=None, min_interval=600):
    """并行补齐多个指数的基差到最新交易日 T，返回 {code: 是否补到 T}。

    三个指数的基差互不依赖（不同现货/不同期货品种），akshare 无连接互斥
    （不同于 thsdk 的进程级连接），可安全并行。原实现在自动更新循环里逐个串行
    调用 ensure_basis_fresh，每个 30-45 秒、合计约 2 分钟，用户中途关闭程序时
    后面的指数基差就永远停在旧交易日（实测每次只有部分指数补到 T）。
    并行后整批耗时≈单个指数的耗时，且一次调用即可保证三指数都被处理到。"""
    codes = [str(c) for c in (codes if codes is not None else FUTURE_PREFIX)]
    if not codes:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    with ThreadPoolExecutor(max_workers=min(3, len(codes))) as ex:
        futs = {c: ex.submit(ensure_basis_fresh, c, min_interval) for c in codes}
        for c, f in futs.items():
            try:
                out[c] = bool(f.result())
            except Exception as e:
                _dbg(f'refresh_all_basis {c} ERR {type(e).__name__}: {e}')
                out[c] = False
    _dbg(f'refresh_all_basis done {out}')
    return out


def _aggregate_highlow(rows_map):
    """事件化聚合新高/新低：每只股票只记录实际发生事件的日期，再按日期归并。
    逻辑与原实现逐日一致：close > 前20日最高(不含当日) 记 high，< 前20日最低记 low。
    返回 {day: {'high': [code...], 'low': [code...]}}，含全部交易日键（无事件日为空列表），
    替代原 O(交易日×股数) 全量扫描（如中证2000：2000股 × 98日 → 大量纯空判断）。

    实现：逐股直接抽数组 + pandas rolling（向量化），不做 DataFrame 构造——
    原实现每只股票 pd.DataFrame(rows).set_index()（list-of-dicts 逐行解析），
    中证2000 2000 股每次 compute 都要重复一遍，实测是该步骤的主要耗时；
    NaN 参与比较结果为 False，与原 notna 掩码口径完全一致。"""
    all_days = set()
    ev = {}
    for code, rows in rows_map.items():
        if not rows:
            ev[code] = ([], [])
            continue
        dates = [r['时间'] for r in rows]
        closes = np.array([r['收盘价'] for r in rows], dtype=float)
        s = pd.Series(closes)
        prev_high = s.shift(1).rolling(20).max().to_numpy()
        prev_low = s.shift(1).rolling(20).min().to_numpy()
        with np.errstate(invalid='ignore'):
            hi_mask = closes > prev_high
            lo_mask = closes < prev_low
        ev[code] = ([dates[i] for i in np.flatnonzero(hi_mask)],
                    [dates[i] for i in np.flatnonzero(lo_mask)])
        all_days.update(dates)
    stats = {d: {'high': [], 'low': []} for d in sorted(all_days)}
    for code, (hi_days, lo_days) in ev.items():
        for d in hi_days:
            stats[d]['high'].append(code)
        for d in lo_days:
            stats[d]['low'].append(code)
    return stats


def compute(idx_code, progress_cb=None, force=False, on_latest=None, refresh_basis=False):
    """计算指数新高/新低统计。
    refresh_basis=True 时（后台增量刷新重算用）强制联网补齐最新交易日 T 的基差；
    正常渲染走渲染级（T-1）缓存，避免启动联网。"""
    cons = load_cons(idx_code)
    cons_map = dict(cons)
    rows_map = ensure_fresh(cons, progress_cb, force=force)

    # 事件化聚合：每只股票只记录发生新高/新低的日期，再按日期归并
    stats = _aggregate_highlow(rows_map)
    all_days = sorted(stats)

    target_days = [d for d in all_days if d >= TARGET_FROM]
    # 手动"立即刷新"(force=True) 也应强制联网补齐基差：否则点击刷新后成分股已到
    # 最新交易日，基差却仍走渲染级(T-1)缓存、永远停在旧值不更新。
    basis_map = get_basis_series(idx_code, force=(refresh_basis or force))
    day_list = []
    for d in target_days:
        day_list.append({
            'date': d,
            'basis': basis_map.get(d),
            'high': [{'code': c, 'name': cons_map.get(c, c)} for c in stats[d]['high']],
            'low': [{'code': c, 'name': cons_map.get(c, c)} for c in stats[d]['low']],
        })

    if str(idx_code).startswith('URFI'):
        disp_name = block_display_name(idx_code)
    else:
        disp_name = INDEXES[idx_code][0]
    meta = {
        'index': idx_code, 'name': disp_name,
        'n': len(rows_map), 'total': len(cons),
        'period': f"{target_days[0]} ~ {target_days[-1]}" if target_days else '-',
    }

    # B方案：渲染级数据（>=T-1）已足够立即显示；若尚未到最新交易日T，
    # 提交后台增量刷新，界面先渲染、最新T静默补拉完成后经 on_latest 回调刷新。
    # 注意：基差快速路径采用渲染级（T-1）容忍，即使成分股已到T，基差缓存仍可能
    # 只覆盖到T-1；此时必须同样提交后台补T，否则最新交易日基差将一直显示'-'。
    if on_latest is not None and not force and not _in_bg_get():
        todo_latest = [c for c, r in rows_map.items()
                       if r and not cache_latest(r)]
        basis_stale = basis_map.get(_latest_trading_date()) is None
        if todo_latest or basis_stale:
            started = _bg_submit(idx_code, todo_latest, cons_map, on_latest)
            meta['latest_refreshing'] = started

    return {'days': day_list, 'meta': meta}


# ---- 板块 OBV 逐日强势统计（只看板块指数本身，不看个股） ----
def _block_daily_metrics(df):
    """按板块指数日K（日期/收盘价/成交量）逐日计算 OBV 强势与 60 日线状态。
    规则：OBV[0]=0；收盘涨 OBV[i]=OBV[i-1]+成交量[i]，跌则减，平不变。
    强势：当日 OBV > OBV20日均线 且 当日 OBV > 5个交易日前 OBV。
    60日线：当日收盘价 > 收盘价60日简单移动平均（含当日）。
    返回 {日期: (obv_strong, above60)}，数据不足（<61个交易日）返回 None"""
    df = df.sort_values('日期').reset_index(drop=True)
    # 源端个股行情字段可能异常（'-'/None/NaN）。收盘价用前值填充（缺失 bar 视为平盘延续），
    # 成交量必须按 0 处理：NaN 一旦进入 cumsum 会让 OBV 从该日起永久变成 NaN，
    # 该板块此后再也判不出强势（原实现未处理，属静默错误）。
    closes = pd.to_numeric(df['收盘价'], errors='coerce').ffill().fillna(0.0).astype(float).values
    vols = pd.to_numeric(df['成交量'], errors='coerce').fillna(0.0).astype(float).values
    dates = df['日期'].astype(str).tolist()
    n = len(closes)
    if n < 61:
        return None
    # OBV 向量化：diff>0 累加成交量，diff<0 累减，平盘不变（OBV[0]=0）
    diff = np.diff(closes)
    obv = np.concatenate([[0.0], np.cumsum(
        np.where(diff > 0, vols[1:], np.where(diff < 0, -vols[1:], 0.0)))])
    obv_ma20 = pd.Series(obv).rolling(20).mean().values
    ma60 = pd.Series(closes).rolling(60).mean().values
    out = {}
    for i in range(n):
        if pd.isna(obv_ma20[i]):
            continue
        strong = obv[i] > obv_ma20[i] and i >= 5 and obv[i] > obv[i - 5]
        above60 = (not pd.isna(ma60[i])) and closes[i] > ma60[i]
        out[dates[i]] = (bool(strong), bool(above60))
    return out


def _build_block_daily_stats(krows):
    """汇总板块逐日统计：daily[date] 含两个计数 + 当日板块名称列表。
    每条记录：obv_strong 强势且未站上60日线数（已剔除）/
    obv_strong_above60 强势且站上60日线数（被剔除）/
    obv_strong_names 剔除后强势板块名称列表 / obv_above60_names 被剔除板块名称列表"""
    daily = {}
    total_ok = 0
    for _code, name, data in krows:
        if not data:
            continue
        m = _block_daily_metrics(pd.DataFrame(data))
        if m is None:
            continue
        total_ok += 1
        for d, (strong, above60) in m.items():
            if d < TARGET_FROM:
                continue
            rec = daily.setdefault(d, {'obv_strong': 0, 'obv_strong_above60': 0,
                                       'obv_strong_names': [], 'obv_above60_names': []})
            if strong:
                if above60:
                    # 站上60日线：从强势统计中剔除，单独计入"被剔除"栏
                    rec['obv_strong_above60'] += 1
                    rec['obv_above60_names'].append(name)
                else:
                    rec['obv_strong'] += 1
                    rec['obv_strong_names'].append(name)
    days = [{'date': d, 'obv_strong': v['obv_strong'],
             'obv_strong_above60': v['obv_strong_above60'],
             'obv_strong_names': v['obv_strong_names'],
             'obv_above60_names': v['obv_above60_names']}
            for d, v in sorted(daily.items())]
    meta = {
        'index': 'BLOCK_OBV_DAILY', 'name': '板块OBV强势统计',
        'n': total_ok, 'total': len(krows),
        'period': f"{days[0]['date']} ~ {days[-1]['date']}" if days else '-',
    }
    return {'days': days, 'meta': meta}


def _block_today_ths_quotes(block_codes, last_close_map):
    """用 thsdk 补齐板块指数今日实时行情（akshare 数据源延迟缺当天K线）。
    串行调用 market_data_block：基础数据(成交量) -> sleep 1.2s -> 扩展(涨幅)。
    今日收盘 = 昨日收盘 × (1 + 涨幅/100)；返回 {code: {'日期','收盘价','成交量'}}，个别失败跳过。
    """
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    result = {}
    if not block_codes:
        return result
    try:
        with ths_session() as ths:
            base = ths.market_data_block(block_codes, query_key='基础数据')
            time.sleep(1.2)
            ext = ths.market_data_block(block_codes, query_key='扩展')
        vol_map = {}
        for _, r in base.df.iterrows():
            try:
                vol_map[str(r['代码'])] = float(r['成交量'])
            except (TypeError, ValueError):
                # 单板块成交量字段异常（'-'/None/NaN）跳过，不影响其余板块
                continue
        for _, r in ext.df.iterrows():
            code = str(r['代码'])
            last_close = last_close_map.get(code)
            if last_close is None or code not in vol_map:
                continue
            try:
                chg = float(r['涨幅'])
            except (TypeError, ValueError):
                continue
            close = round(last_close * (1 + chg / 100.0), 4)
            result[code] = {'日期': today, '收盘价': close, '成交量': vol_map[code]}
    except Exception as e:
        print(f'[obv_daily] ths today quotes ERR {e}', flush=True)
    return result


def _block_via_ths_incremental(krows, target_date):
    """thsdk 增量补齐板块指数K线到指定交易日：收集末条日期<target 的板块，
    用 market_data_block 当日实时行情以“昨收×(1+涨幅%)”补当日收盘点，
    省去 akshare 全量重拉（90块约3分钟）。任一块失败整体返回 None 由调用方兜底。
    返回补到 target 后的完整 krows；已全到无需补时原样返回。"""
    need_today = []
    last_close_map = {}
    for code, name, data in krows:
        if data and data[-1]['日期'] < target_date:
            need_today.append(code)
            last_close_map[code] = float(data[-1]['收盘价'])
    if not need_today:
        return krows
    quotes = _block_today_ths_quotes(need_today, last_close_map)
    # 覆盖率不足时整体放弃补当日：若只补到少数板块，逐日统计会在当日用不完整数据
    # 计数（明显偏低），且 meta 仍会被标为最新交易日造成"假新鲜"，多数板块永远停
    # 在上一日。
    if not quotes or len(quotes) < _BLOCK_TODAY_MIN_COVER * len(need_today):
        _dbg(f'block inc today insufficient {len(quotes) if quotes else 0}/'
             f'{len(need_today)} -> skip')
        return None
    out = []
    for code, name, data in krows:
        q = quotes.get(code)
        if q and (not data or data[-1]['日期'] < q['日期']):
            data = list(data) + [q]
        out.append((code, name, data))
    return out


def _cached_block_stats():
    """从 block_kline 缓存直接构建逐日统计（不联网）。用于全量重建被节流时退回
    现有缓存结果，避免数据源滞后时界面显示空白。失败返回 None。"""
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        rows = con.execute("SELECT code, name, data FROM block_kline").fetchall()
        con.close()
        if not rows:
            return None
        krows = [(c, n, json.loads(d)) for c, n, d in rows]
        return _build_block_daily_stats(krows)
    except Exception as e:
        _dbg(f'cached block stats ERR {type(e).__name__}: {e}')
        return None


def get_block_obv_daily(force=False):
    """逐日统计板块指数 OBV 强势：返回 {'days': [{'date','obv_strong','obv_strong_above60'}], 'meta'}。
    数据：90个同花顺行业板块指数日K（akshare ak.stock_board_industry_index_ths，只看板块指数本身）。
    缓存：block_kline 表 + meta.block_obv_daily_date（= 实际写入数据的真实最新日期）。
    meta 记录的是源端真实最新日期而非"最新交易日"；
    源端未出更新则命中缓存秒出；源端更新则自动全量重建，保证板块自动更新到源可得最新。
    统计区间：TARGET_FROM 起；首次联网较慢，二次缓存秒出。
    """
    # 模块级节流时间戳必须在函数内声明 global：下方有赋值（_BLOCK_REBUILD_TS = ...），
    # 未声明时它被当作局部变量，上方节流判断读取它就会抛
    # UnboundLocalError，导致板块数据在"缓存未命中、需要全量重建"时永远加载失败。
    global _BLOCK_REBUILD_TS
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    latest = _latest_trading_date()
    row = con.execute("SELECT value FROM meta WHERE key='block_obv_daily_date'").fetchone()
    if not force and row and row[0]:
        krows = con.execute("SELECT code, name, data FROM block_kline").fetchall()
        con.close()
        if krows:
            krows = [(c, n, json.loads(d)) for c, n, d in krows]
            stats = _build_block_daily_stats(krows)
            # 防"假新鲜"：meta 标为最新交易日但数据实际未含该日时
            # （如上次 thsdk 补今日行情失败仍写入 meta），强制重新联网更新。
            # 命中要求缓存数据实际日期与 meta 一致，且源端未出现更新日期。
            if stats['days'] and stats['days'][-1]['date'] == row[0]:
                if row[0] >= latest:
                    # 缓存已覆盖最近一个完整交易日：源端不可能有更新，
                    # 直接命中（免 thsdk 板块列表 + akshare 探针两次联网）
                    return stats
                # 缓存落后于最新交易日（跨日/收盘后首次运行）。
                # 注意：akshare 板块指数源端收盘后常滞后一日，不能以其日期判定
                # “源端未更新”（否则 thsdk 补当日路径永不触发，板块永远停在 T-1）。
                # 当日已收盘（>=15:10）且最新交易日即今天时，用 thsdk 增量补当日；
                # 补不到（盘中/未出/失败）则命中现有缓存，等下一轮自动更新。
                today = datetime.now(TZ).strftime('%Y-%m-%d')
                now = datetime.now(TZ)
                prev_td = _prev_trading_date(latest)
                cache_last = stats['days'][-1]['date'] if stats['days'] else None
                # 仅当缓存末条紧邻最新交易日（== prev_td，只缺 latest 一个点）时，
                # thsdk 增量补当日才安全；若中间缺交易日（缓存末条 < prev_td，如缓存
                # 停08-31而latest=09-02缺09-01），增量会跳过缺失日致板块K线日期不连续
                # 且永不回补，必须 fall through 到 akshare 全量重建（源端滞后一日亦可
                # 捕获缺失日）。
                if cache_last != prev_td:
                    _dbg(f'block cache gap -> rebuild: latest={latest} cache_last={cache_last} '
                         f'prev_td={prev_td}')
                elif latest == today and now.hour * 60 + now.minute >= 15 * 60 + 10:
                    inc = _block_via_ths_incremental(krows, latest)
                    if inc is not None:
                        stats2 = _build_block_daily_stats(inc)
                        if stats2['days'] and stats2['days'][-1]['date'] == latest:
                            # 写库并更新 meta（数据已含 latest → 后续直接命中秒出）
                            con2 = sqlite3.connect(DB)
                            con2.execute('PRAGMA busy_timeout=5000')
                            for c, n, dd in inc:
                                con2.execute(
                                    "INSERT OR REPLACE INTO block_kline(code,name,data,ts) VALUES(?,?,?,?)",
                                    (c, n, json.dumps(dd, ensure_ascii=False), time.time()))
                            con2.execute(
                                "INSERT OR REPLACE INTO meta(key,value) VALUES('block_obv_daily_date',?)",
                                (latest,))
                            con2.commit()
                            con2.close()
                            _dbg(f'block incremental->latest {latest} OK ({len(inc)} blocks)')
                            return stats2
                    _dbg(f'block inc today unavailable (latest={latest} row0={row[0]}) -> keep cache')
                    return stats
                elif latest == today:
                    # 盘中：今日数据源端必无，保留缓存，等收盘后(>=15:10)增量补当日
                    _dbg(f'block cache intraday keep: latest={latest} now={now.strftime("%H:%M")}')
                    return stats
                else:
                    # 缓存落后于已完成的过去交易日（跨日，如最新T=昨日08-31而缓存停在08-28）。
                    # 源端(akshare 板块指数)收盘后滞后一日，此刻重查可捕捉缺失交易日，
                    # 走全量重建补上，避免板块永远停在更早日期。
                    _dbg(f'block cache stale -> rebuild: latest={latest} row0={row[0]} '
                         f'now={now.strftime("%H:%M")}')
                    # fall through 到下方 akshare 全量重建
    con.close()

    # 节流：非 force 且距上次全量重建过近时，退回缓存结果。
    # 触发场景：akshare 板块指数源端收盘后滞后一日，且 thsdk 当日行情又不可用时，
    # 板块 meta 始终落后于最新交易日 -> watchdog 每 2 分钟判定一次"陈旧"并触发全量
    # 重建（90 块 × 联网约 1-3 分钟），反复空转。这里限制重建频率，缓存结果仍可显示。
    if not force:
        if time.time() - _BLOCK_REBUILD_TS < _BLOCK_REBUILD_MIN_SEC:
            cached_stats = _cached_block_stats()
            if cached_stats is not None:
                _dbg('block rebuild throttled -> return cache')
                return cached_stats
    _BLOCK_REBUILD_TS = time.time()

    import akshare as ak  # 局部导入，避免启动变慢
    today = datetime.now(TZ)
    # 拉足够长历史（约10个月），确保 TARGET_FROM 之前有 60 个以上交易日，60日均线从统计首日起即有值
    start = (today - timedelta(days=300)).strftime('%Y%m%d')
    end = today.strftime('%Y%m%d')
    blocks = load_block_list()
    _dbg(f'get_block_obv_daily blocks={len(blocks)} latest={latest}')
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    krows = []
    need_today = []        # 最新日期缺今天的板块
    last_close_map = {}    # {code: akshare最后一日收盘价}
    last_date_map = {}     # {code: akshare最后一日日期}
    for code, name in blocks:
        try:
            df = ak.stock_board_industry_index_ths(
                symbol=name, start_date=start, end_date=end)
            if df is None or df.empty:
                raise ValueError('empty df')
            df = df.sort_values('日期')
            df['日期'] = pd.to_datetime(df['日期']).dt.strftime('%Y-%m-%d')
            data = df[['日期', '收盘价', '成交量']].to_dict('records')
            if data:
                last_date_map[code] = data[-1]['日期']
                if data[-1]['日期'] < latest:
                    need_today.append(code)
                    last_close_map[code] = float(data[-1]['收盘价'])
            krows.append((code, name, data))
        except Exception as e:
            _dbg(f'block {name}({code}) ERR {type(e).__name__}: {e}')
            print(f'[obv_daily] {name} ERR {e}', flush=True)
        time.sleep(0.3)
    # akshare 数据源延迟缺当天 -> 用 thsdk 补齐今日行情点，追加到板块K线序列末尾。
    # 仅当 akshare 末条紧邻最新交易日（只缺 latest 一天）时才补当日：缺多日时直接
    # 追加今日会造成日期断层（中间交易日永远缺失且无法回补），必须放弃补点。
    if need_today:
        prev_td = _prev_trading_date(latest)
        adjacent = [c for c in need_today if last_date_map.get(c) == prev_td]
        if len(adjacent) < len(need_today):
            _dbg(f'block akshare gap >1 day -> skip thsdk today patch '
                 f'({len(adjacent)}/{len(need_today)} adjacent)')
        else:
            today_quotes = _block_today_ths_quotes(need_today, last_close_map)
            if today_quotes and len(today_quotes) >= _BLOCK_TODAY_MIN_COVER * len(need_today):
                fixed = 0
                for i, (code, name, data) in enumerate(krows):
                    q = today_quotes.get(code)
                    if q:
                        krows[i] = (code, name, data + [q])
                        fixed += 1
                _dbg(f'block today quotes fixed {fixed}/{len(need_today)}')
            else:
                _dbg(f'block today quotes insufficient '
                     f'{len(today_quotes) if today_quotes else 0}/{len(need_today)} -> skip today')
    for code, name, data in krows:
        con.execute("INSERT OR REPLACE INTO block_kline(code,name,data,ts) VALUES(?,?,?,?)",
                    (code, name, json.dumps(data, ensure_ascii=False), time.time()))
    stats = _build_block_daily_stats(krows)
    # meta 记录实际数据的真实最新日期（thsdk 补今日失败时可能仍是上一交易日），
    # 供下次启动按探针源端日期判定缓存新鲜度，避免"假新鲜"与每日重复全量重建
    real_latest = stats['days'][-1]['date'] if stats['days'] else latest
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('block_obv_daily_date',?)",
                (real_latest,))
    con.commit()
    con.close()
    return stats


# ---- 大盘每日情绪统计（同花顺 i 问财 wencai_nlp，含ST口径） ----
# 口径与用户确认的表格一致：红盘/绿盘/实际涨停(含ST)/非一字涨停/跌停(含ST)/
# 炸板(问财口径，含ST)/连板(连续涨停天数>=2)/二连板/三连板/三连板以上(>=4)/
# 最高板(非ST股票的连续涨停天数最大值)
def _cn_date(date_str):
    """'2026-08-26' -> '2026年8月26日'"""
    y, m, d = date_str.split('-')
    return f'{int(y)}年{int(m)}月{int(d)}日'


def _wencai_query(ths, cond, retries=4):
    """问财查询：成功返回 DataFrame（可能0行=当日无此情形）；
    接口返回 success=False（限频/超时等，df为空但不抛异常）或抛异常时
    重试，仍失败返回 None（与空结果严格区分，防止误记为0）"""
    last_err = None
    for attempt in range(retries):
        try:
            resp = ths.wencai_nlp(cond)
            if resp is not None and getattr(resp, 'success', True):
                df = resp.df
                return df if df is not None else pd.DataFrame()
            last_err = getattr(resp, 'error', 'success=False')
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
        time.sleep(0.8 * (attempt + 1))
    _dbg(f'wencai ERR {cond}: {last_err}')
    return None


def _find_col(df, key):
    """按关键词模糊匹配列名（问财列名带日期后缀，如 '涨停类型[20260826]'）"""
    for c in df.columns:
        if key in str(c):
            return c
    return None


def _query_market_day(ths, date_str):
    """单日大盘情绪统计：5条问财查询。
    返回 dict（12项统计）；五项全为空结果返回 'skip'（非交易日）；
    任一查询失败返回 None（调用方保留待下次重试）。"""
    cn = _cn_date(date_str)
    # 逐条查询并在首条失败时立即中止：连接不可用/限频时后续几条必然同样失败，
    # 原实现先把 5 条全部跑完（每条内部重试 4 轮约 8 秒）再用 any() 判断，
    # 一个失败的日期要白等约 30 秒。
    dfs = []
    for cond in (f'{cn}涨跌幅大于0的股票',
                 f'{cn}涨跌幅小于0的股票',
                 f'{cn}涨停的股票，连续涨停天数，涨停类型',
                 f'{cn}跌停的股票',
                 f'{cn}炸板的股票'):
        if dfs:
            time.sleep(0.4)
        df = _wencai_query(ths, cond)
        if df is None:
            return None
        dfs.append(df)
    if all(len(df) == 0 for df in dfs):
        return 'skip'  # 非交易日：问财无任何记录
    df_red, df_green, df_zt, df_dt, df_zb = dfs

    rec = {'red': len(df_red), 'green': len(df_green),
           'zt': len(df_zt), 'dt': len(df_dt), 'zhaban': len(df_zb)}
    rec['fzt'] = rec['zt']
    rec['yizi'] = 0
    rec['lb'] = 0
    rec['lb2'] = 0
    rec['lb3'] = 0
    rec['lb3p'] = 0
    rec['high_lb'] = 0
    if len(df_zt) > 0:
        lb_col = _find_col(df_zt, '连续涨停天数')
        type_col = _find_col(df_zt, '涨停类型')
        name_col = _find_col(df_zt, '股票简称') or _find_col(df_zt, '股票名称')
        # 整列预解析（替代逐行构造 pd.Series，历史回填明显提速）
        n_zt = len(df_zt)
        if lb_col is not None:
            lb_nums = pd.to_numeric(df_zt[lb_col], errors='coerce').tolist()
            lb_strs = df_zt[lb_col].astype(str).tolist()
        else:
            lb_nums = [float('nan')] * n_zt
            lb_strs = [''] * n_zt
        types = df_zt[type_col].astype(str).tolist() if type_col is not None else [''] * n_zt
        names = df_zt[name_col].astype(str).tolist() if name_col is not None else [''] * n_zt
        for i in range(n_zt):
            if lb_col is not None and pd.notna(lb_nums[i]):
                lb_days = int(lb_nums[i])
            else:
                # 中文数字或文本：按字符串解析，失败按首板计
                try:
                    lb_days = int(lb_strs[i].strip())
                except Exception:
                    lb_days = 1
            if '一字' in types[i]:
                rec['yizi'] += 1
            if lb_days >= 2:
                rec['lb'] += 1
            if lb_days == 2:
                rec['lb2'] += 1
            elif lb_days == 3:
                rec['lb3'] += 1
            elif lb_days >= 4:
                rec['lb3p'] += 1
            # 最高板：剔除 ST/*ST
            if not names[i].startswith(('ST', '*ST')):
                rec['high_lb'] = max(rec['high_lb'], lb_days)
    rec['fzt'] = rec['zt'] - rec['yizi']
    # 一致性校验（防接口异常导致错误数据入库）：
    # 涨停股必为红盘、跌停股必为绿盘、连板股必为涨停子集
    if rec['red'] < rec['zt'] or rec['green'] < rec['dt'] or rec['lb'] > rec['zt']:
        _dbg(f'market_daily {date_str} inconsistent: {rec} -> retry next run')
        return None
    return rec


def _market_trade_dates():
    """统计区间交易日列表：优先用交易日历（已由 _calendar 缓存），
    失败回退 klines 缓存日期并集。返回 ['YYYY-MM-DD', ...]（升序，不含未来日期）"""
    latest = _latest_trading_date()
    days = _calendar()
    if days:
        i = bisect.bisect_right(days, latest)
        j = bisect.bisect_left(days, TARGET_FROM)
        return days[j:i]
    # 回退：取 klines 缓存中若干只股票的日期并集。
    # 原写法 LIMIT 20 可能正好取到长期停牌/已剔除成分的孤儿记录（日期停在旧
    # 交易日），导致回退出的日期集合不含最新交易日；这里扩大样本并并入
    # market_daily 已统计的交易日（该表由 get_market_daily 维护，日期准确）。
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        rows = con.execute("SELECT data FROM klines LIMIT 300").fetchall()
        days = set()
        try:
            for (d,) in con.execute("SELECT date FROM market_daily"):
                days.add(d)
        except Exception:
            pass
        con.close()
        for (data,) in rows:
            try:
                for r in json.loads(data):
                    days.add(r['时间'])
            except Exception:
                continue
        return sorted(d for d in days if TARGET_FROM <= d <= latest)
    except Exception:
        return []


def _market_volume_cached():
    """只读市场成交总额缓存 {date: 万亿float}，绝不联网回填。
    用于 get_market_daily 渲染秒出：成交总额缺失时先显示 '-'，
    由后台线程 _market_volume_backfill 补全后回调界面刷新。"""
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        rows = con.execute("SELECT date, vol FROM market_volume").fetchall()
        con.close()
        return {d: v for d, v in rows}
    except Exception:
        return {}


def market_volume_needs_backfill():
    """成交总额缓存是否未覆盖最近交易日、存在历史缺口或口径非 official（需后台回填）。"""
    try:
        latest = _latest_trading_date()
    except Exception:
        latest = None
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        row = con.execute("SELECT value FROM meta WHERE key='market_volume_src'").fetchone()
        con.close()
        official = bool(row and row[0] == 'official')
    except Exception:
        official = False
    if not official:
        return True
    cache = _market_volume_cached()
    if latest is not None and latest not in cache:
        return True
    # 历史缺口：交易日历中任一交易日缺失（如程序多日未运行/中间某日接口失败）
    try:
        dates = _market_trade_dates()
    except Exception:
        dates = []
    for d in dates:
        if d not in cache:
            return True
    return False


# 成交总额后台回填：多调用方共享一个后台线程，回填完成后统一回调（GUI 侧刷新）
_VOL_BG_LOCK = threading.Lock()
_VOL_BG_ACTIVE = False
_VOL_BG_CBS = []


def request_volume_backfill(on_done=None):
    """请求后台回填市场成交总额；若已有回填线程在跑则把 on_done 挂到完成回调列表。
    幂等：无论调用多少次，同一时刻只跑一个回填线程。on_done 在回填完成后于后台线程调用。"""
    global _VOL_BG_ACTIVE
    with _VOL_BG_LOCK:
        if on_done is not None:
            _VOL_BG_CBS.append(on_done)
        if _VOL_BG_ACTIVE:
            return False
        _VOL_BG_ACTIVE = True

    def _run():
        # 嵌套函数不会继承外层的 global 声明：这里必须自己声明，否则下方赋值会创建
        # 局部变量，模块级 _VOL_BG_ACTIVE 永远保持 True，之后的回填请求全部被"幂等"挡掉
        global _VOL_BG_ACTIVE
        try:
            _market_volume_map(force=False)
        except Exception as e:
            _dbg(f'volume backfill ERR {type(e).__name__}: {e}')
        finally:
            with _VOL_BG_LOCK:
                _VOL_BG_ACTIVE = False
                cbs = _VOL_BG_CBS[:]
                _VOL_BG_CBS.clear()
            for cb in cbs:
                try:
                    cb()
                except Exception as e:
                    _dbg(f'volume backfill cb ERR {type(e).__name__}: {e}')

    threading.Thread(target=_run, daemon=True).start()
    return True


def _market_volume_map(force=False):
    """每日市场成交总额（沪深两市股票成交金额之和，单位：万亿元），缓存 market_volume 表。
    口径：上交所 stock_sse_deal_daily 股票成交金额 + 深交所 stock_szse_summary 股票成交金额，
    两市相加换算万亿（保留2位）。返回 {date: 万亿float}。
    快速路径：口径为 official 且缓存无任何缺口（含历史交易日）则免联网；
    否则逐日回填缺失交易日（TARGET_FROM 起约100+日，约2-4分钟，后台执行）。"""
    try:
        latest = _latest_trading_date()
    except Exception:
        latest = None
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        row = con.execute("SELECT value FROM meta WHERE key='market_volume_src'").fetchone()
        rows = con.execute("SELECT date, vol FROM market_volume").fetchall()
        con.close()
        official = bool(row and row[0] == 'official')
        cache = {d: v for d, v in rows}
    except Exception:
        cache = {}
        official = False
    try:
        dates = _market_trade_dates()
    except Exception:
        dates = []
    if not dates:
        dates = sorted(d for d in cache if (latest is None or d <= latest))
    todo = [d for d in dates if d not in cache]
    if force and latest is not None and latest not in todo and latest in dates:
        todo.append(latest)
    # 兜底：无论交易日历是否可用，只要最新交易日 T 未缓存就必须补。
    # 日历接口失败时 dates 会回退成 klines 前20只股票的日期并集，而这些股票
    # 可能是已从成分股剔除、长期停牌的孤儿记录（库里存在停在 08-25 的 192 只），
    # 于是 T 根本不在 dates 里、todo 为空，成交额一次网络请求都不发就"回填完成"，
    # 最新交易日成交额永远显示'-'（用户反馈"成交总额没有自动更新"的直接原因）。
    if latest is not None and latest not in cache and latest not in todo:
        todo.append(latest)
    # 快速路径：口径正确且无任何缺口（含中间交易日）才免联网直接返回。
    # 原实现只判 latest 是否在缓存，中间某日缺失（接口失败/程序多日未运行）时
    # 仍误判为新鲜而跳过回填，导致这些交易日成交额永久显示'-'。
    if not force and official and not todo:
        return cache
    if not official:
        # 旧口径数据（腾讯源指数成交额）作废，清空重拉
        try:
            con = sqlite3.connect(DB)
            con.execute('PRAGMA busy_timeout=5000')
            con.execute("DELETE FROM market_volume")
            con.commit()
            con.close()
        except Exception:
            pass
        cache = {}
        todo = [d for d in dates]
    if todo:
        import akshare as ak
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        n_ok = 0
        consec_fail = 0
        for d in todo:
            try:
                d_compact = d.replace('-', '')
                sse = ak.stock_sse_deal_daily(date=d_compact)
                amt_sse = float(sse[sse['单日情况'] == '成交金额']['股票'].values[0])  # 亿元
                szse = ak.stock_szse_summary(date=d_compact)
                amt_szse = float(szse[szse['证券类别'] == '股票']['成交金额'].values[0]) / 1e8  # 元→亿元
                total_yi = amt_sse + amt_szse  # 亿元
                v = round(total_yi / 1e4, 4)   # 万亿元
                con.execute("INSERT OR REPLACE INTO market_volume(date,vol,ts) VALUES(?,?,?)",
                            (d, v, time.time()))
                cache[d] = v
                n_ok += 1
                consec_fail = 0
            except Exception as e:
                _dbg(f'market_volume {d} ERR {type(e).__name__}: {e}')
                # 熔断：交易所接口不可用时空转上百天毫无意义
                consec_fail += 1
                if consec_fail >= MARKET_MAX_CONSEC_FAIL:
                    _dbg(f'market_volume abort after {consec_fail} consecutive failures')
                    break
        # 只有至少成功一天才标记为 official：全部失败时也标 official，
        # 会让后续判定误以为缓存已是权威口径而停止重试
        if n_ok:
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('market_volume_src','official')")
        con.commit()
        con.close()
    return cache


def get_market_daily(force=False, progress_cb=None):
    """大盘每日情绪统计（问财口径，含ST）。
    缓存：market_daily 表（valid=0 表示非交易日，避免重复探测）。
    快速路径：缓存已覆盖最近一个完整交易日（且非 force）时完全不联网
    ——跳过交易日历接口与问财查询，直接从缓存返回（启动秒出）。
    缓存落后时：todo = 未缓存日期逐日回填；force=True 额外重查最近一个
    交易日（立即刷新用）。写库使用单连接批量提交。
    返回 {'days': [{'date','red','green','zt','yizi','fzt','dt','zhaban',
    'lb','lb2','lb3','lb3p','high_lb'}], 'meta': {...}}"""
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    con.execute("CREATE TABLE IF NOT EXISTS market_daily("
                "date TEXT PRIMARY KEY, valid INTEGER, red INTEGER, green INTEGER,"
                "zt INTEGER, yizi INTEGER, fzt INTEGER, dt INTEGER, zhaban INTEGER,"
                "lb INTEGER, lb2 INTEGER, lb3 INTEGER, lb3p INTEGER, high_lb INTEGER, ts REAL)")
    cached = {}
    for row in con.execute("SELECT date, valid, red, green, zt, yizi, fzt, dt, zhaban,"
                           " lb, lb2, lb3, lb3p, high_lb FROM market_daily"):
        cached[row[0]] = row
    con.close()

    latest = _latest_trading_date()
    todo = []
    # 快速路径判定：最新交易日已有缓存记录（含 valid=0 的节假日探测记录）即视为新鲜
    if force or latest not in cached:
        dates = _market_trade_dates()
        if not dates:
            dates = sorted(d for d in cached if TARGET_FROM <= d <= latest)
        todo = [d for d in dates if d not in cached]
        if force and latest not in todo:
            todo.append(latest)

    if todo:
        n_done = 0
        consec_fail = 0
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        try:
            for d in todo:
                # 每个交易日开一个独立会话：
                # 1) 不长时间独占连接锁，指数/板块拉取不会被大盘回填饿死；
                # 2) 连接若被异常掐断，下一天重新 connect 即可自愈。
                try:
                    with ths_session() as ths:
                        r = _query_market_day(ths, d)
                except Exception as e:
                    _dbg(f'market_daily {d} session ERR {type(e).__name__}: {e}')
                    r = None
                if r is None:
                    consec_fail += 1
                    _dbg(f'market_daily {d} query failed, skip for next run')
                    # 熔断：问财连续不可用（限频/未连接/休市）时立即中止，
                    # 否则上百个日期每个都要空转重试 4 轮，白等数十分钟。
                    if consec_fail >= MARKET_MAX_CONSEC_FAIL:
                        _dbg(f'market_daily abort after {consec_fail} consecutive failures')
                        break
                else:
                    consec_fail = 0
                    if r == 'skip':
                        con.execute(
                            "INSERT OR REPLACE INTO market_daily"
                            "(date,valid,red,green,zt,yizi,fzt,dt,zhaban,"
                            "lb,lb2,lb3,lb3p,high_lb,ts) "
                            "VALUES(?,0,0,0,0,0,0,0,0,0,0,0,0,0,?)", (d, time.time()))
                        cached[d] = (d, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, time.time())
                    else:
                        con.execute(
                            "INSERT OR REPLACE INTO market_daily"
                            "(date,valid,red,green,zt,yizi,fzt,dt,zhaban,"
                            "lb,lb2,lb3,lb3p,high_lb,ts) "
                            "VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (d, r['red'], r['green'], r['zt'], r['yizi'], r['fzt'],
                             r['dt'], r['zhaban'], r['lb'], r['lb2'], r['lb3'],
                             r['lb3p'], r['high_lb'], time.time()))
                        cached[d] = (d, 1, r['red'], r['green'], r['zt'], r['yizi'],
                                     r['fzt'], r['dt'], r['zhaban'], r['lb'], r['lb2'],
                                     r['lb3'], r['lb3p'], r['high_lb'], time.time())
                    con.commit()
                n_done += 1
                if progress_cb:
                    progress_cb(n_done, len(todo))
        finally:
            con.close()

    # 从（已含本次写入的）内存缓存直接构建，省一次全表扫描
    day_list = []
    # 成交总额：只读缓存秒出（缺失显示'-'），不阻塞大盘渲染；
    # 若缓存未覆盖最新交易日，交由 GUI 侧 request_volume_backfill 后台补全后刷新
    vol_map = _market_volume_cached()
    for d, row in cached.items():
        if d < TARGET_FROM or not row[1]:
            continue
        day_list.append({'date': d, 'red': row[2], 'green': row[3], 'zt': row[4],
                         'yizi': row[5], 'fzt': row[6], 'dt': row[7], 'zhaban': row[8],
                         'lb': row[9], 'lb2': row[10], 'lb3': row[11], 'lb3p': row[12],
                         'high_lb': row[13], 'vol': vol_map.get(d)})
    day_list.sort(key=lambda x: x['date'])
    meta = {
        'index': 'MARKET_DAILY', 'name': '大盘情绪统计',
        'n': len(day_list), 'total': len(day_list),
        'period': f"{day_list[0]['date']} ~ {day_list[-1]['date']}" if day_list else '-',
    }
    return {'days': day_list, 'meta': meta}


def _build_market_detail(date_str, rows):
    """从 market_detail 缓存行构建明细 dict。
    rows 元素形如 (kind, code, name, lb_days, xtype, valid)"""
    for r in rows:
        if r[0] == 'skip':
            return {'date': date_str, 'skip': True}
    out = {'date': date_str, 'zt': [], 'dt': [], 'zb': []}
    for kind, code, name, lb, xtype, valid in rows:
        if not valid:
            continue
        item = {'code': code, 'name': name}
        if kind == 'zt':
            item['lb_days'] = lb
            item['xtype'] = xtype
        out[kind].append(item)
    return out


def get_market_day_detail(date_str, force=False):
    """按交易日查询大盘明细个股（涨停/跌停/炸板，涨停行含连板天数与涨停类型），带缓存。
    缓存表 market_detail：(date,kind,code) 唯一，kind in (zt/dt/zb)，valid=0 表示非交易日。
    缓存命中直接返回；否则联网问财查询（3条）逐行入库。
    返回 {'date', 'zt':[...], 'dt':[...], 'zb':[...]} 或 {'date','skip':True}（非交易日）；
    任一查询失败返回 None（调用方保留待下次重试）。"""
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    con.execute("CREATE TABLE IF NOT EXISTS market_detail("
                "date TEXT, kind TEXT, code TEXT, name TEXT,"
                "lb_days INTEGER, xtype TEXT, valid INTEGER, ts REAL,"
                "PRIMARY KEY(date, kind, code))")
    rows = con.execute("SELECT kind, code, name, lb_days, xtype, valid"
                       " FROM market_detail WHERE date=?", (date_str,)).fetchall()
    con.close()
    if rows and not force:
        return _build_market_detail(date_str, rows)

    cn = _cn_date(date_str)
    with ths_session() as ths:
        df_zt = _wencai_query(ths, f'{cn}涨停的股票，连续涨停天数，涨停类型')
        if df_zt is None:
            return None  # 连接不可用：不必再跑后两条，交给下次重试
        time.sleep(0.4)
        df_dt = _wencai_query(ths, f'{cn}跌停的股票')
        time.sleep(0.4)
        df_zb = _wencai_query(ths, f'{cn}炸板的股票')
    if df_zt is None or df_dt is None or df_zb is None:
        return None
    if len(df_zt) == 0 and len(df_dt) == 0 and len(df_zb) == 0:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        con.execute("INSERT OR REPLACE INTO market_detail"
                    "(date,kind,code,name,lb_days,xtype,valid,ts)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (date_str, 'skip', '', '', 0, '', 0, time.time()))
        con.commit()
        con.close()
        return {'date': date_str, 'skip': True}

    ins = []  # 元素: (kind, code, name, lb_days, xtype, valid)
    for kind, df in (('zt', df_zt), ('dt', df_dt), ('zb', df_zb)):
        if df is None or len(df) == 0:
            continue
        code_col = _find_col(df, '股票代码')
        name_col = _find_col(df, '股票简称') or _find_col(df, '股票名称')
        n = len(df)
        codes = (df[code_col].astype(str).tolist() if code_col is not None
                 else [str(i + 1) for i in range(n)])
        names = (df[name_col].astype(str).tolist() if name_col is not None
                 else [''] * n)
        if kind == 'zt':
            lb_col = _find_col(df, '连续涨停天数')
            type_col = _find_col(df, '涨停类型')
            lb_nums = (pd.to_numeric(df[lb_col], errors='coerce').tolist()
                       if lb_col is not None else [float('nan')] * n)
            lb_strs = (df[lb_col].astype(str).tolist()
                       if lb_col is not None else [''] * n)
            types = (df[type_col].astype(str).tolist()
                     if type_col is not None else [''] * n)
            for i in range(n):
                if pd.notna(lb_nums[i]):
                    lb = int(lb_nums[i])
                else:
                    try:
                        lb = int(lb_strs[i].strip())
                    except Exception:
                        lb = 1
                ins.append(('zt', codes[i], names[i], lb, types[i], 1))
        else:
            for i in range(n):
                ins.append((kind, codes[i], names[i], 0, '', 1))
    if not ins:
        return None
    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=5000')
    if force:
        # 强制重查：先清掉该日旧记录，避免已不在列表中的个股（如炸板回封）残留
        con.execute("DELETE FROM market_detail WHERE date=?", (date_str,))
    con.executemany("INSERT OR REPLACE INTO market_detail"
                    "(date,kind,code,name,lb_days,xtype,valid,ts)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    [(date_str,) + t + (time.time(),) for t in ins])
    con.commit()
    con.close()
    return _build_market_detail(date_str, ins)


# ---- 两融：沪深京三市融资余额（同花顺融资融券页面口径，只要融资余额） ----
# 数据源（单位已统一为"亿元"）：
#   沪市：交易所汇总 akshare stock_margin_sse（区间一次取回，原始单位元）
#   深市：akshare stock_margin_szse（只支持单日，需逐日补齐，原始单位亿元）
#   京市：北交所官网 rzrqjyyexxController/summaryInfoResult.do（JSONP，原始单位元）
#   交叉校验：2026-10-22 沪12469.38 + 深11721.62 + 京78.13 = 24269.13 亿元，
#            与东方财富两融历史合计 RZYE 24269.14 亿元一致（口径正确）。
_BSE_MARGIN_URL = 'https://www.bse.cn/rzrqjyyexxController/summaryInfoResult.do'
_BSE_REFERER = 'https://www.bse.cn/disclosure/rzrq_trans_list.html'
# ponytail: 全局非阻塞锁，够用——两融补齐只在冷启动/每日更新触发，
# 若将来出现多路高频合并需求，再按日期分段细化
_MARGIN_LOCK = threading.Lock()
_MARGIN_RETRY_TTL = 6 * 3600        # 历史缺市日重拉节流：6 小时内不重复联网
# 两融数据当日并不即时发布（沪深京一般当晚陆续出数），且交易所接口偶发 SSL
# 断连/空响应。最近几个交易日若沿用 6 小时节流，一次偶发失败就会让该日合计
# 整天显示 "-"，要等 6 小时后才重试（"两融数据未更新"的直接根因）。
# 因此最近 N 个交易日改用分钟级节流，让失败日/未发布日快速自愈。
_MARGIN_RETRY_TTL_RECENT = 180      # 最近交易日缺市日：3 分钟即重试
_MARGIN_RECENT_N = 3                # "最近"窗口（交易日个数）
_MARGIN_FORCE_RECENT_N = 5          # 手动刷新时强制重查的最近交易日个数


def _bse_margin_day(date_str):
    """北交所（京市）单日融资余额（亿元）。非交易日/无数据返回 None。

    北交所官网偶发 ReadTimeout/连接重置（并发补齐时更明显），
    单次失败即落空会让当日合计整天为 "-"，故取数做 3 次退避重试。
    """
    try:
        import ast
        import re

        import requests
        r = None
        last = None
        for attempt in range(3):
            try:
                r = requests.get(_BSE_MARGIN_URL,
                                 params={'callback': 'cb', 'transDate': date_str,
                                         'page': '0'},
                                 headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                                        'AppleWebKit/537.36 Chrome/138.0.0.0',
                                          'Referer': _BSE_REFERER},
                                 timeout=30)
                break
            except Exception as e:
                last = e
                if attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
        if r is None:
            _dbg(f'margin bse {date_str} ERR {type(last).__name__}: {last}')
            return None
        m = re.search(r'^[^(]+\((.*)\)\s*$', r.text, flags=re.S)
        if not m:
            return None
        # JSONP 负载本身是合法字面量（末尾日期字段用单引号、json 解析不了），
        # literal_eval 直接吃下；原先用正则只补最后一个字段，响应一改就静默失败
        j = ast.literal_eval(m.group(1))[0]
        if not j:
            return None
        v = j[0].get('rzye')
        if v is None:
            return None
        return round(float(v) / 1e8, 4)      # 元 -> 亿元
    except Exception as e:
        _dbg(f'margin bse {date_str} ERR {type(e).__name__}: {e}')
        return None


def _sse_margin_range(start, end):
    """沪市融资余额区间：{date: 亿元}（一次请求覆盖整段区间）。

    akshare 在区间内一行都没有时会抛 "Length mismatch"（=当日尚未发布），
    这属于正常情况不是网络故障，直接返回空；其余异常（连接重置/超时）退避重试。
    """
    import akshare as ak
    df = None
    for attempt in range(3):
        try:
            df = ak.stock_margin_sse(start_date=start.replace('-', ''),
                                    end_date=end.replace('-', ''))
            break
        except ValueError as e:
            if 'Length mismatch' in str(e):
                return {}          # 区间内无数据（未发布），非故障
            if attempt == 2:
                raise
        except Exception:
            if attempt == 2:
                raise
        time.sleep(0.8 * (attempt + 1))
    if df is None:
        return {}
    out = {}
    for _, r in df.iterrows():
        raw = str(r['信用交易日期'])
        d = raw if '-' in raw else f'{raw[:4]}-{raw[4:6]}-{raw[6:8]}'
        try:
            out[d] = round(float(r['融资余额']) / 1e8, 4)
        except (TypeError, ValueError):
            continue
    return out


_SZSE_MARGIN_URL = 'https://www.szse.cn/api/report/ShowReport/data'
_SZSE_REFERER = 'https://www.szse.cn/disclosure/margin/margin/index.html'


def _szse_margin_day(date_compact):
    """深市单日融资余额（亿元）。非交易日/当日未发布返回 None。

    直连深交所"融资融券交易总量"报表接口，不再经 akshare：
    akshare 的实现对空响应直接抛 ValueError 且无重试，交易所偶发 SSL 断连时
    会被误判为"无数据"落库，使当日合计长期显示 "-"。
    接口字段 jrrzye 单位已是亿元（含千分位逗号）。
    """
    import requests
    last = None
    for attempt in range(3):
        try:
            r = requests.get(
                _SZSE_MARGIN_URL,
                params={'SHOWTYPE': 'JSON', 'CATALOGID': '1837_xxpl',
                        'txtDate': date_compact, 'tab1PAGENO': '1',
                        'random': str(time.time())},
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                       'AppleWebKit/537.36 Chrome/138.0.0.0',
                         'Referer': _SZSE_REFERER},
                timeout=20)
            j = r.json()
            rows = (j[0].get('data') or []) if j else []
            if not rows:
                return None          # 当日尚未发布（非异常，无需重试）
            return round(float(str(rows[0]['jrrzye']).replace(',', '')), 4)
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    _dbg(f'margin szse {date_compact} ERR {type(last).__name__}: {last}')
    return None


def _margin_pull_day(d):
    """并发任务：拉某一交易日的深市/京市融资余额，返回 (date, sz, bj)。"""
    return d, _szse_margin_day(d.replace('-', '')), _bse_margin_day(d)


def _margin_cached():
    """读两融缓存 {date: (sh, sz, bj, total, ts)}（不联网）。"""
    try:
        con = sqlite3.connect(DB)
        con.execute('PRAGMA busy_timeout=5000')
        rows = con.execute(
            "SELECT date, sh, sz, bj, total, ts FROM margin_daily").fetchall()
        con.close()
        return {r[0]: tuple(r[1:]) for r in rows}
    except Exception as e:
        _dbg(f'margin cache read ERR {type(e).__name__}: {e}')
        return {}


def _margin_view(cache):
    """缓存 -> {'days','meta'}：chg = 合计较上一个有效交易日的增减（亿元）。"""
    day_list = []
    prev_total = None
    for d in sorted(cache):
        if d < TARGET_FROM:
            continue
        sh, sz, bj, total = cache[d][:4]
        if sh is None and sz is None and bj is None:
            continue    # 三市均未发布（如当日盘前/未出数）：不展示空行，数据到后自动出现
        chg = None
        if total is not None and prev_total is not None:
            chg = round(total - prev_total, 4)
        if total is not None:
            prev_total = total
        day_list.append({'date': d, 'sh': sh, 'sz': sz, 'bj': bj,
                         'total': total, 'chg': chg})
    return {
        'days': day_list,
        'meta': {
            'index': 'MARGIN_DAILY', 'name': '两融融资余额（沪深京）',
            'n': len(day_list), 'total': len(day_list),
            'period': f"{day_list[0]['date']} ~ {day_list[-1]['date']}" if day_list else '-',
        },
    }


def get_margin_daily(force=False, progress_cb=None):
    """沪深京三市融资余额逐日统计（亿元）。

    返回 {'days': [{'date','sh','sz','bj','total','chg'}], 'meta'}；
    total 仅在沪深京三市都取到时才有值，缺任一市记 None（界面显示'-'）——
    用残缺数据算合计会出现万亿级虚假跳变，且落库后即永久错误。
    联网补齐由 _MARGIN_LOCK 串行：并发调用方直接读缓存返回，不重复拉。
    """
    cache = _margin_cached()
    try:
        latest = _latest_trading_date()
    except Exception:
        latest = None
    dates = [d for d in _market_trade_dates() if d >= TARGET_FROM]
    if latest and latest >= TARGET_FROM:
        dates = sorted(set(dates) | {latest})
    now = time.time()
    recent = set(dates[-_MARGIN_RECENT_N:])
    # 缺市日（total 为 None）按 TTL 节流重拉；三市齐全的历史日永不再联网。
    # 最近几个交易日改用分钟级 TTL：两融当晚才陆续发布、交易所接口偶发失败，
    # 沿用 6 小时节流会让当日合计整天显示 "-"，直到下次启动才可能补上。
    todo = [d for d in dates
            if d not in cache
            or (cache[d][3] is None
                and now - cache[d][4] > (_MARGIN_RETRY_TTL_RECENT if d in recent
                                         else _MARGIN_RETRY_TTL))]
    if force:
        # 手动刷新：最近若干交易日的缺市日一律重拉（不受 TTL 限制）。
        # 原实现只重拉最新交易日，点【立即刷新】也补不上更早的缺市日（如
        # 深市当日偶发失败导致合计为 "-" 的那天），用户只能等 6 小时。
        force_days = {d for d in dates[-_MARGIN_FORCE_RECENT_N:]
                      if d not in cache or cache[d][3] is None}
        if latest and latest >= TARGET_FROM:
            force_days.add(latest)
        todo = sorted(set(todo) | force_days)

    if todo and _MARGIN_LOCK.acquire(blocking=False):
        try:
            sse = {}
            try:
                sse = _sse_margin_range(todo[0], max(todo))
            except Exception as e:
                _dbg(f'margin sse ERR {type(e).__name__}: {e}')
            # 深市/京市只有单日接口，并发 8 补齐
            from concurrent.futures import ThreadPoolExecutor
            n = len(todo)
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(_margin_pull_day, todo))
            con = sqlite3.connect(DB)
            con.execute('PRAGMA busy_timeout=5000')
            for i, (d, sz, bj) in enumerate(results, 1):
                if progress_cb:
                    progress_cb(i, n)
                sh = sse.get(d)
                miss = None in (sh, sz, bj)
                if miss and sh is None and sz is None and bj is None and d in cache:
                    continue    # 三市全空且已有历史值：网络故障，不把好数据覆盖成空
                ts = time.time()
                con.execute("INSERT OR REPLACE INTO margin_daily"
                            "(date,sh,sz,bj,total,ts) VALUES(?,?,?,?,?,?)",
                            (d, sh, sz, bj,
                             None if miss else round(sh + sz + bj, 4), ts))
                cache[d] = (sh, sz, bj, None if miss else round(sh + sz + bj, 4), ts)
            con.commit()
            con.close()
            _dbg(f'margin daily fetched {len(results)}/{n} days')
        finally:
            _MARGIN_LOCK.release()
    return _margin_view(cache)


def margin_needs_refresh():
    """是否需要联网补齐（纯查询）：T-1 缺失，或缺市日超过 TTL 没重试过。

    两融数据常在交易日当晚才发布：若按"T 缺失"判定，watchdog 会每轮都认为
    过期、反复拉起全量每日更新；若完全不管缺市日，数据发布后又没人再去拉。
    折中只用"缺市日的 ts（=上次尝试时间）是否超过 TTL"来重试，等于每 6 小时
    自动补一次，不会高频触发自动更新。
    """
    cache = _margin_cached()
    try:
        ref = _prev_trading_date(_latest_trading_date())
    except Exception:
        return False
    if ref not in cache:
        return True
    now = time.time()
    return any(v[3] is None and now - v[4] > _MARGIN_RETRY_TTL
               for d, v in cache.items() if d >= ref)


def margin_recent_incomplete():
    """最近 N 个交易日是否仍有合计缺失（纯查询，不联网）。

    供界面看门狗判断要不要静默重试：两融当日数据当晚才发布，只看"最新日缺失"
    会在发布前反复空转，因此限定在最近几个交易日；TTL 节流交给 get_margin_daily。
    """
    try:
        cache = _margin_cached()
        dates = [d for d in _market_trade_dates() if d >= TARGET_FROM]
        latest = _latest_trading_date()
        if latest >= TARGET_FROM:
            dates = sorted(set(dates) | {latest})
        return any(cache.get(d, (None,) * 5)[3] is None
                   for d in dates[-_MARGIN_RECENT_N:])
    except Exception as e:
        _dbg(f'margin recent incomplete ERR {type(e).__name__}: {e}')
        return False
