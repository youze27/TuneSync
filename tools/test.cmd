@echo off
rem Backend test suite on the dev machine (Python from .pylibs).
rem See tunesync/README.md for what these cover.
rem Docker equivalents: tools\docker-test.cmd
setlocal
set ROOT=%~dp0..
set PYTHONPATH=%ROOT%\.pylibs
cd /d "%ROOT%"
python -m pytest tunesync\tests -q %*
