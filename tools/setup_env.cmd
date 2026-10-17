@echo off
rem Install runtime + test dependencies into <repo>\.pylibs.
rem .pylibs is self-contained: the suite passes even with system
rem site-packages stripped from sys.path.
rem See tunesync/README.md for details.
setlocal
set ROOT=%~dp0..
set LIBS=%ROOT%\.pylibs

python -m pip install --no-input --disable-pip-version-check --upgrade --target "%LIBS%" -r "%ROOT%\tunesync\requirements-dev.txt"
if errorlevel 1 exit /b 1

python -c "import sys;sys.path.insert(0,r'%LIBS%');import fastapi,uvicorn,yaml,requests,mutagen,apscheduler,loguru,pytest,httpx;print('deps ok: fastapi',fastapi.__version__)"
