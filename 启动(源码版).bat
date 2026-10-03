@echo off
chcp 65001 >nul

rem 以源码方式启动改动版 Bili23 Downloader。
rem PYSTAND_HOME 必须指向本目录：程序用它定位自带的 bundle\ffmpeg.exe。
rem 与安装版共用同一份 %APPDATA%\Bili23 Downloader\config.json，
rem 因此下载目录、命名规则、任务队列都接着用；但两者不能同时开。

cd /d "%~dp0"
set "PYSTAND_HOME=%~dp0"

start "" "%~dp0..\..\..\.venv\Scripts\pythonw.exe" "%~dp0src\main.py"
