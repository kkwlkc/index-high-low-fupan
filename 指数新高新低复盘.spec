# -*- mode: python ; coding: utf-8 -*-
import os
import sysconfig

from PyInstaller.utils.hooks import collect_data_files

# site-packages 动态推导（原为写死本机绝对路径，换机器/换用户即打包失败）
_SITE = sysconfig.get_paths()['purelib']
datas = [
    ('sz50_cons.csv', '.'),
    ('sz300_cons.csv', '.'),
    ('zz2000_cons.csv', '.'),
    (os.path.join(_SITE, 'py_mini_racer'), 'py_mini_racer'),
    (os.path.join(_SITE, 'thsdk', 'libs'), 'thsdk/libs'),
]
# 运行数据副本（可选）：仅当本机存在时打包，便于首次打开即有行情缓存。
# 开源仓库不包含该约 40MB 的 klines_idx.db，缺失时跳过即可正常构建
_db = os.path.join('..', '运行版', 'klines_idx.db')
if os.path.exists(_db):
    datas.append((_db, '.'))
datas += collect_data_files('akshare')


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='指数新高新低复盘',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='指数新高新低复盘',
)
