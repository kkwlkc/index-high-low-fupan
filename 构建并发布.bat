@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  一键构建并发布
rem  源码目录 -> PyInstaller 打包 -> 发布到 ..\运行版（保留运行数据）
rem ============================================================

set "PY=C:\Users\11145\AppData\Local\Programs\Python\Python312\python.exe"
set "APP_NAME=指数新高新低复盘"
if not exist "%PY%" set "PY=python"
set "DIST=%~dp0dist"
set "OUT=%~dp0..\运行版"

echo ============================================
echo  构建 %APP_NAME%
echo  源码: %~dp0
echo  发布: %OUT%
echo ============================================
echo.

rem ---- 0. 检查是否正在运行 ----
tasklist /fi "imagename eq %APP_NAME%.exe" 2>nul | find /i "%APP_NAME%.exe" >nul
if not errorlevel 1 (
    echo [错误] 检测到 %APP_NAME%.exe 正在运行，请先关闭程序再打包。
    echo.
    pause
    exit /b 1
)

rem ---- 1. 回归测试 ----
echo [1/3] 基差回归测试...
"%PY%" "%~dp0tests\test_basis_robustness.py"
if errorlevel 1 (
    echo.
    echo [错误] 回归测试未通过，已中止打包。
    pause
    exit /b 1
)
echo.

rem ---- 2. 打包 ----
echo [2/3] PyInstaller 打包中（约 1-3 分钟）...
"%PY%" -m PyInstaller "%APP_NAME%.spec" --noconfirm
if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请查看上方日志。
    pause
    exit /b 1
)
echo.

rem ---- 3. 发布到运行版 ----
echo [3/3] 发布到运行版（保留 klines_idx.db 等运行数据）...
if not exist "%OUT%" mkdir "%OUT%"
copy /y "%DIST%\%APP_NAME%\%APP_NAME%.exe" "%OUT%\" >nul
robocopy "%DIST%\%APP_NAME%\_internal" "%OUT%\_internal" /MIR /NFL /NDL /NJH /NJS /NP >nul
if not exist "%OUT%\klines_idx.db" (
    rem PyInstaller 6 的 onedir 布局把 datas 放进 _internal；程序首次启动也会
    rem 自动从 _internal 复制一份到 exe 同目录，这里只是省掉那一次复制
    if exist "%DIST%\%APP_NAME%\_internal\klines_idx.db" (
        copy /y "%DIST%\%APP_NAME%\_internal\klines_idx.db" "%OUT%\" >nul
        echo   已初始化运行数据 klines_idx.db
    )
)

echo.
echo ============================================
echo  完成。运行版内容：
dir /b "%OUT%"
echo ============================================
echo.
pause
