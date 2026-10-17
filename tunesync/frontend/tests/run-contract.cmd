@echo off
rem Frontend API-layer integration check against a live backend.
rem
rem   tests\contract.mjs loads src\api.js verbatim (only DOM deps and baseURL
rem   are swapped), then drives the real backend with the app's own request
rem   functions. Missing endpoints, wrong verbs, wrong field names and broken
rem   password-masking semantics all blow up here; Python's TestClient cannot
rem   reach this layer.
rem
rem   1) start the backend first:  tools\run.cmd
rem   2) run this script (defaults to http://127.0.0.1:8066)
rem
rem   node tests\contract.mjs [baseURL]
setlocal
set ROOT=%~dp0..
cd /d "%~dp0.."

node tests\contract.mjs %1
exit /b %ERRORLEVEL%
