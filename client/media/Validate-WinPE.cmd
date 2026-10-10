@echo off
set R=%SystemDrive%\re-pxe
set W=%R%\work
for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do if exist %%D:\REPXE-RESULTS.flag set RESULTS=%%D:
%R%\repxe_core_tests.exe > %W%\core.txt 2>&1
if errorlevel 1 goto fail
%R%\re-pxe.exe locate --path %R% > %W%\location.json 2>%W%\error.json
if errorlevel 1 goto fail
for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do if exist %%D:\sources\boot.wim set MEDIA=%%D:
if not defined MEDIA goto fail
%R%\re-pxe.exe inspect-image --image %MEDIA%\sources\boot.wim > %W%\image.json 2>%W%\error.json
if errorlevel 1 goto fail
copy %MEDIA%\re-pxe-fixture.wim %W%\fixture.wim >nul
if errorlevel 1 goto fail
%R%\re-pxe.exe plan --config %R%\plan-config.json > %W%\plan.json 2>%W%\error.json
if errorlevel 1 goto fail
echo PASS > %W%\status.txt
goto save
:fail
echo FAIL > %W%\status.txt
:save
if defined RESULTS copy %W%\*.json %RESULTS%\ >nul
if defined RESULTS copy %W%\*.txt %RESULTS%\ >nul
wpeutil shutdown
goto :eof
