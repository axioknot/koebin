@echo off
chcp 65001 >nul
cd /d %~dp0
title こえびん - 操作画面

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (python --version >nul 2>&1 && set "PY=python")
if not defined PY (
    echo Python が見つかりませんでした。
    echo python.org から Python をインストールしてから、もう一度実行してください。
    echo.
    pause
    exit /b 1
)

echo こえびんの操作画面を起動します。ブラウザが自動で開きます。
echo 終わるときは、この画面で Ctrl+C を押してください。
echo.
%PY% koebin_ui.py --config config.json
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo 操作画面を終了しました。
) else (
    echo エラーで終了しました（コード %RC%）。上の表示を確認してください。
)
pause
exit /b %RC%
