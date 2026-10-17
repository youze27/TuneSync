@echo off
rem æå¼åç®å½çæå»ºæéæä»¶åæ­¥å° tunesync\deploy\ï¼èªåå« NAS é¨ç½²ç®å½ï¼ã
rem
rem å¼åæºç ä»çå¨ tunesync\appãtunesync\frontend ç­åä½ï¼æ¬å°è¿è¡ã
rem pytestãtools\run.cmd é½ä¾èµ tunesync.app è¿ä¸ªåè·¯å¾ï¼ï¼deploy\ éæ¾çæ¯
rem æå»ºéåç¨çå¯æ¬ãæ¹å®ä»£ç ãåå¤é¨ç½²å° NAS åè·ä¸æ¬¡æ¬èæ¬ï¼ç¶åææ´ä¸ª
rem tunesync\deploy æä»¶å¤¹æ·å° NASï¼å¨éé¢ `docker compose up -d --build` å³å¯ã
rem
rem ç¨æ³ï¼
rem   tools\pack-deploy.cmd
setlocal
set ROOT=%~dp0..
set SRC=%ROOT%\tunesync
set DST=%SRC%\deploy

echo == syncing app/ config/ frontend/ tests/ into tunesync\deploy ==

rem /MIR ä¿è¯å è¿çæä»¶å¨å¯æ¬éä¹æ¶å¤±ï¼/NFL /NDL åªçæè¦ä¸å·å±ï¼
rem /XD æé¤ç¼å­ä¸æå»ºäº§ç©ï¼app\static ç±éåå pnpm build éæ°çæï¼
rem frontend\node_modules ä¸æåã
robocopy "%SRC%\app"      "%DST%\app"      /MIR /XD __pycache__ static /XF *.pyc /NFL /NDL
robocopy "%SRC%\config"   "%DST%\config"   /MIR /XF *.db *.log /NFL /NDL
robocopy "%SRC%\frontend" "%DST%\frontend" /MIR /XD node_modules /XF *.log /NFL /NDL
robocopy "%SRC%\tests"    "%DST%\tests"    /MIR /XD __pycache__ .pytest_cache .work /XF *.pyc /NFL /NDL
if errorlevel 8 (
  echo robocopy failed with exit code %errorlevel%
  exit /b 1
)

copy /Y "%SRC%\requirements.txt"         "%DST%\" >nul
copy /Y "%SRC%\requirements-dev.txt"     "%DST%\" >nul
copy /Y "%SRC%\pytest.ini"               "%DST%\" >nul

echo.
echo Done. tunesync\deploy is ready to copy to the NAS.
