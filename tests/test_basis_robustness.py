# -*- coding: utf-8 -*-
"""基差"持续自愈"机制离线回归测试。

覆盖三类防复发场景：
  1) 数据未齐（当日仅 3/4 合约）：不得落库当日、渲染不展示、需刷新=True；
  2) 数据齐备（4/4）：落库当日并携带就绪证据 n/ref，需刷新=False；
  3) 失真中间态已入库（n<ref / 缺证据）：即便覆盖关通过，质量关仍判定
     需刷新 → 自动更新会联网重算覆盖，实现持续自愈（不依赖一次性清理）。

全部离线运行：不访问新浪/腾讯，期货与现货数据由 mock 提供。
运行：python tests/test_basis_robustness.py
"""
import io
import os
import sys
import sqlite3
import shutil
import tempfile
import unittest

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 工程根目录由脚本位置推导（原写死旧路径 D:\指数新高新低复盘，迁移后失效）
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)
import pandas as pd
import server

LATEST = '2026-09-09'
PREV = '2026-09-08'
DAYS = ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', PREV, LATEST]

FAKE = {'spot': None, 'fmap': None, 'fcnt': None}   # 当前场景数据


def _fake_spot_index_daily(spot, latest):
    return FAKE['spot'], 'test'


def _fake_fut_weighted_daily(pref, start, end, refresh_to=None, active_since=None):
    return FAKE['fmap'], FAKE['fcnt']


def _make_default():
    # 现货收盘与期货加权价：基差 = close - wclose
    spot = pd.DataFrame({'date': DAYS,
                         'close': [3000.0, 3001.0, 3002.0, 3003.0, 3004.0, 3005.0]})
    fmap = {d: 2950.0 for d in DAYS}
    fcnt = {d: 4 for d in DAYS}
    return spot, fmap, fcnt


class BasisRobustnessTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='basis_test_')
        # 隔离数据目录：避免 init_db 覆盖工程真实 DB、避免写坏缓存
        server.RES_DIR = cls.tmp
        server.DATA_DIR = cls.tmp
        server.CONS_DIR = cls.tmp
        server.DB = os.path.join(cls.tmp, 'klines_idx.db')
        # 交易日固定，避免联网拉日历
        server._latest_trading_date = staticmethod(lambda: LATEST)
        server._prev_trading_date = staticmethod(lambda t: PREV)
        # 数据源全部注入 mock，测试完全离线
        server._spot_index_daily = _fake_spot_index_daily
        server._fut_weighted_daily = _fake_fut_weighted_daily
        server.init_db()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server._BASIS_CACHE.clear()
        server._FUT_CONTRACT_CACHE.clear()
        con = sqlite3.connect(server.DB)
        con.execute('DELETE FROM basis_series')
        con.commit()
        con.close()
        self._set_data(*_make_default())

    def _set_data(self, spot, fmap, fcnt):
        FAKE['spot'] = spot
        FAKE['fmap'] = fmap
        FAKE['fcnt'] = fcnt

    def _rows(self):
        con = sqlite3.connect(server.DB)
        out = con.execute('SELECT date, basis, n, ref FROM basis_series '
                          'WHERE code="000300" ORDER BY date').fetchall()
        con.close()
        return out

    # ---------- 场景1：数据未齐，不得落库/展示 ----------
    def test_partial_contracts_do_not_store_latest(self):
        spot, fmap, fcnt = _make_default()
        fcnt[LATEST] = 3                      # 当日只有 3/4 合约
        self._set_data(spot, fmap, fcnt)
        out = server.get_basis_series('000300', force=True)
        self.assertNotIn(LATEST, out, '数据未齐时当日基差不应返回')
        self.assertTrue(server.basis_needs_refresh('000300'),
                        '数据未齐时应保持"需刷新"，由自动更新继续重试')

    def test_ready_contracts_store_latest_with_evidence(self):
        spot, fmap, fcnt = _make_default()    # 4/4 齐备
        self._set_data(spot, fmap, fcnt)
        out = server.get_basis_series('000300', force=True)
        self.assertIn(LATEST, out)
        self.assertAlmostEqual(out[LATEST], 3005.0 - 2950.0, places=2)
        rows = self._rows()
        latest_row = [r for r in rows if r[0] == LATEST][0]
        self.assertEqual(latest_row[2], 4, '当日行应记录参与合约数 n')
        self.assertEqual(latest_row[3], 4, '当日行应记录期望合约数 ref')
        self.assertFalse(server.basis_needs_refresh('000300'),
                         '齐备入库后不应再触发联网刷新')

    # ---------- 场景2：失真中间态已入库，质量关仍判定需刷新 ----------
    def test_distorted_row_is_detected_by_quality_gate(self):
        for n, ref, expect in ((2, 4, True),    # 部分合约失真的当日行
                               (None, None, True),   # 旧库行无就绪证据
                               (4, 4, False),   # 齐备行
                               (4, 0, False)):  # 无历史基准时按就绪处理
            with self.subTest(n=n, ref=ref):
                con = sqlite3.connect(server.DB)
                con.execute('DELETE FROM basis_series WHERE code="000300"')
                con.execute('INSERT OR REPLACE INTO basis_series'
                            '(code,date,basis,ts,n,ref) VALUES("000300",?,?,?,?,?)',
                            (LATEST, 1.0, 0.0, n, ref))
                con.commit()
                con.close()
                self.assertEqual(server.basis_needs_refresh('000300'), expect)

    # ---------- 场景3：失真行可被"重算覆盖"自愈 ----------
    def test_self_heal_recompute_overwrites_distorted_row(self):
        # 先造一个失真当日行（模拟历史版本已落库）
        con = sqlite3.connect(server.DB)
        con.execute('INSERT OR REPLACE INTO basis_series'
                    '(code,date,basis,ts,n,ref) VALUES("000300",?,?,?,?,?)',
                    (LATEST, 28.74, 0.0, 2, 4))
        con.commit()
        con.close()
        self.assertTrue(server.basis_needs_refresh('000300'))
        # 自动更新联网重算（数据已齐）
        out = server.get_basis_series('000300', force=True)
        self.assertAlmostEqual(out[LATEST], 55.0, places=2)
        latest_row = [r for r in self._rows() if r[0] == LATEST][0]
        self.assertEqual(latest_row[1], 55.0, '失真值应被重算覆盖')
        self.assertEqual((latest_row[2], latest_row[3]), (4, 4))
        self.assertFalse(server.basis_needs_refresh('000300'))

    # ---------- 渲染快速路径：不展示失真当日值 ----------
    def test_fast_path_hides_bad_latest_until_recomputed(self):
        con = sqlite3.connect(server.DB)
        for d, basis in ((PREV, 54.0), (LATEST, 28.74)):
            con.execute('INSERT OR REPLACE INTO basis_series'
                        '(code,date,basis,ts,n,ref) VALUES("000300",?,?,?,?,?)',
                        (d, basis, 0.0, 4 if d == PREV else 2,
                         4 if d == PREV else 4))
        con.commit()
        con.close()
        server._BASIS_CACHE.clear()
        out = server.get_basis_series('000300', force=False)
        self.assertIn(PREV, out)
        self.assertNotIn(LATEST, out, '渲染层不得展示失真当日值（应显-）')
        # 后台补齐正确行后，快速路径恢复展示
        con = sqlite3.connect(server.DB)
        con.execute('UPDATE basis_series SET n=4, ref=4 WHERE code="000300" AND date=?',
                    (LATEST,))
        con.commit()
        con.close()
        server._BASIS_CACHE.clear()
        out = server.get_basis_series('000300', force=False)
        self.assertIn(LATEST, out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
