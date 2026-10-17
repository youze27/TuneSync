@echo off
rem Run the backend test suite inside Docker.
rem
rem Not redundant with test.cmd: the dev machine runs Python 3.14 while
rem the image runs 3.12, and dependency resolution, datetime behaviour and
rem APScheduler trigger timing can differ between them.
rem
rem Usage:
rem   tools\docker-test.cmd [registry-prefix] [pip-index-url]
rem Defaults use mainland-China mirrors (docker.io / pypi.org time out there).
rem On an overseas network pass the upstream sources, e.g.
rem   tools\docker-test.cmd docker.io/library https://pypi.org/simple
rem
rem See tunesync/README.md for what the suite covers.
setlocal
set REGISTRY=%~1
set PIP_INDEX_URL=%~2
if "%REGISTRY%"=="" set REGISTRY=docker.1ms.run/library
if "%PIP_INDEX_URL%"=="" set PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

set ROOT=%~dp0..
cd /d "%ROOT%"

rem æå»ºä¸ä¸ææ¯èªåå«é¨ç½²ç®å½ tunesync\deployï¼åæå¼åç®å½çæºç åæ­¥è¿å»ã
call "%~dp0pack-deploy.cmd"
if errorlevel 1 exit /b 1

echo == building test image (base: %REGISTRY%) ==
docker build --build-arg REGISTRY=%REGISTRY% --build-arg PIP_INDEX_URL=%PIP_INDEX_URL% --target test -t tunesync:test -f tunesync\deploy\Dockerfile tunesync\deploy
if errorlevel 1 exit /b 1

echo == running tests in container (Python 3.12) ==
docker run --rm tunesync:test
if errorlevel 1 exit /b 1

docker images tunesync:test --format "{{.Repository}}:{{.Tag}}  {{.Size}}"
