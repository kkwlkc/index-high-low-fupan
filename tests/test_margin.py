# -*- coding: utf-8 -*-
"""两融（沪深京融资余额）健壮性离线测试（test stub 版本）。

覆盖三类防复发场景：
  1) 三市齐全：合计 = 三者之和，较前一日增减正确；
  2) 缺任一市（如深市单日接口失败）：合计记 None，且不会用残缺数据拼出
     万亿级虚假跳变——界面显示'-'、该日不参与 chg 基准；
  3) 缺市日按 TTL 节流重拉，不会被每次调用反复联网；T 日数据滞后发布
     不会让 margin_needs_refresh 恒为真（watchdog 反复拉起每日更新）。

全部离线运行：数据由 mock 提供，DB 指向临时文件。
运行：python tests/test_margin.py
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

DAYS = ['2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22']
LATEST = DAYS[-1]

# 工程根目录由脚本位置推导
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

import server


def _make_row(d, vals):
    """按 {市: 值} 造 mock 源数据"""
    return d, vals.get('sz'), vals.get('bj')


class TestMargin(unittest.TestCase):
    def setUp(self):
        self.orig = {
            'DB': server.DB,
            'pull': server._margin_pull_day,
            'sse': server._sse_margin_range,
            'dates': server._market_trade_dates,
            'latest': server._latest_trading_date,
        }
        self.tmp = tempfile.mktemp(suffix='.db')
        server.DB = self.tmp
        con = sqlite3.connect(self.tmp)
        con.execute("CREATE TABLE IF NOT EXISTS margin_daily("
                    "date TEXT PRIMARY KEY, sh REAL, sz REAL, bj REAL,"
                    "total REAL, ts REAL)")
        con.commit()
        con.close()
        server._market_trade_dates = lambda: list(DAYS)
        server._latest_trading_date = lambda: LATEST
        self.pulled = []          # 记录被联网拉取的日期

        def fake_pull(d):
            self.pulled.append(d)
            return _make_row(d, self.data.get(d, {}))

        server._margin_pull_day = fake_pull
        server._sse_margin_range = lambda s, e: {
            d: v['sh'] for d, v in self.data.items() if 'sh' in v and s <= d <= e}

    def tearDown(self):
        for k, v in self.orig.items():
            setattr(server, k, v) if k != 'DB' else None
        server.DB = self.orig['DB']
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _set(self, data):
        self.data = data

    # ---------- 场景1：三市齐全 ----------
    def test_total_and_chg(self):
        self._set({d: {'sh': 12000.0 + i, 'sz': 11000.0 + i, 'bj': 80.0 + i}
                   for i, d in enumerate(DAYS)})
        out = server.get_margin_daily(force=True)
        days = out['days']
        self.assertEqual(len(days), 4)
        self.assertAlmostEqual(days[0]['total'], 23080.0, places=2)
        self.assertIsNone(days[0]['chg'])
        self.assertAlmostEqual(days[1]['chg'], 3.0, places=2)
        self.assertAlmostEqual(days[-1]['total'], 23089.0, places=2)

    # ---------- 场景2：缺任一市不出合计（回归万亿级虚假跳变） ----------
    def test_missing_market_no_total(self):
        data = {d: {'sh': 12000.0, 'sz': 11000.0, 'bj': 80.0} for d in DAYS}
        del data['2026-10-21']['sz']            # 深市当日取不到
        data['2026-10-21'] = {'sh': 12000.0, 'bj': 80.0}
        self._set(data)
        out = server.get_margin_daily(force=True)
        bad = [d for d in out['days'] if d['date'] == '2026-10-21'][0]
        self.assertIsNone(bad['total'], '缺市日合计必须为 None')
        self.assertIsNone(bad['chg'])
        nxt = [d for d in out['days'] if d['date'] == '2026-10-22'][0]
        prev_ok = [d for d in out['days'] if d['date'] == '2026-10-20'][0]
        self.assertAlmostEqual(nxt['chg'],
                               round(nxt['total'] - prev_ok['total'], 4), places=2)

    # ---------- 场景3：缺市日 TTL 节流，不反复联网 ----------
    def test_missing_day_not_repulled(self):
        self._set({d: {'sh': 12000.0, 'bj': 80.0} for d in DAYS})   # 始终缺深市
        server.get_margin_daily(force=True)
        first = len(self.pulled)
        self.pulled.clear()
        server.get_margin_daily()
        self.assertEqual(self.pulled, [], 'TTL 内不应重复联网拉缺市日')

    # ---------- 场景6：最近交易日用分钟级 TTL，偶发失败/未发布日快速自愈 ----------
    def test_recent_missing_day_short_ttl(self):
        self._set({d: {'sh': 12000.0, 'bj': 80.0} for d in DAYS})   # 始终缺深市
        server.get_margin_daily(force=True)
        self.pulled.clear()
        con = sqlite3.connect(server.DB)
        con.execute("UPDATE margin_daily SET ts=?", (time.time() - 600,))   # 10 分钟前
        con.commit()
        con.close()
        server.get_margin_daily()
        self.assertIn(LATEST, self.pulled,
                      '最近交易日缺市日应在分钟级 TTL 后重试（否则当天合计一直显示 -）')
        self.assertNotIn(DAYS[0], self.pulled, '较早的缺市日仍走 6 小时节流')

    # ---------- 场景5：缺市日超过 TTL 应重新联网（否则当晚发布的数据永远拉不到）----------
    def test_missing_day_retried_after_ttl(self):
        self._set({d: {'sh': 12000.0, 'bj': 80.0} for d in DAYS})   # 缺深市 -> total 为 None
        server.get_margin_daily(force=True)
        con = sqlite3.connect(server.DB)
        con.execute("UPDATE margin_daily SET ts=?", (time.time() - 7 * 3600,))
        con.commit()
        con.close()
        self.assertTrue(server.margin_needs_refresh(), '缺市日超 TTL 应判为需要联网')

    # ---------- 场景4：T 日滞后发布不触发 watchdog ----------
    def test_needs_refresh_ignores_missing_latest(self):
        self._set({d: {'sh': 1.0, 'sz': 1.0, 'bj': 1.0} for d in DAYS[:-1]})
        server.get_margin_daily(force=True)
        self.assertFalse(server.margin_needs_refresh(),
                         'T 日缺失不应判为需要联网（否则 watchdog 反复全量更新）')
        con = sqlite3.connect(server.DB)
        con.execute("DELETE FROM margin_daily WHERE date=?", (DAYS[-2],))
        con.commit()
        con.close()
        self.assertTrue(server.margin_needs_refresh(), 'T-1 缺失应判为需要联网')


if __name__ == '__main__':
    unittest.main(verbosity=2)
