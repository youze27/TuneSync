@echo off
rem Start TuneSync locally (no Docker).
rem Listens on the port in config.yaml (server.port, default 8066).
rem See tunesync/README.md for details.
setlocal
set ROOT=%~dp0..
set PYTHONPATH=%ROOT%\.pylibs
cd /d "%ROOT%"
python -m tunesync.app.main
