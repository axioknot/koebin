@echo off
chcp 65001 >nul
cd /d %~dp0
title こえびん - チェック実行

if not exist "config.json" (
    echo 設定ファイル config.json が見つかりません。
    echo config.example.json を config.json という名前でコピーし、通知先のURLと App ID を書いてから、もう一度実行してください。
    echo.
    pause
    exit /b 1
)

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

echo 新着レビューのチェックを実行します。
echo.
%PY% steam_review_notifier.py --config config.json
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo 完了しました。
) else (
    echo エラーで終了しました（コード %RC%）。上の表示を確認してください。
)
pause
exit /b %RC%
