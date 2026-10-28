@echo off
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 -I -B "%~dp0tools\launch_mkva_tool.pyw"
  exit /b
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw -I -B "%~dp0tools\launch_mkva_tool.pyw"
  exit /b
)
echo Install Python 3.12 or later with Tkinter, then open this launcher again.
pause
