@echo off
cd /d "%~dp0"
python -m llm_arena --open %*
if errorlevel 1 (
  echo.
  echo Az LLM Arena hibaval allt le - a fenti uzenet mutatja az okat.
  pause
)
