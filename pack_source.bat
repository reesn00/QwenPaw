@echo off
setlocal

REM ============================================================
REM  pack_source.bat - create a source snapshot archive (tar.gz)
REM
REM  Keep this script in the repo root (next to pyproject.toml).
REM  It packs the current working copy into "<parent>\<folder>.tar.gz",
REM  e.g. ..\QwenPaw.tar.gz
REM
REM  Non-functional / generated files are excluded (venv, node_modules,
REM  __pycache__, build output, egg-info, caches, IDE & AI agent local
REM  dirs, local env/secrets, OS junk, ...).
REM
REM  .git/ is intentionally kept so repository history is preserved.
REM  Remove the note above / add an exclude if you do not want .git.
REM ============================================================

REM Switch to the directory where this script is located
pushd "%~dp0"

REM Safety check: make sure we are in the repo root
if not exist "pyproject.toml" (
    echo [ERROR] This script must live in the repo root next to pyproject.toml.
    popd
    pause
    exit /b 1
)

REM Folder name (e.g. QwenPaw) doubles as the source dir name
for %%I in ("%CD%") do set "SRC=%%~nxI"

REM Go up one level and create the archive there
cd ..

set "ARCHIVE=%CD%\%SRC%.tar.gz"
if exist "%ARCHIVE%" del /f /q "%ARCHIVE%"

echo Packing "%SRC%" -^> "%ARCHIVE%"
echo (excluding .venv, node_modules, caches, build output, egg-info, IDE/local dirs, ...)

tar -czvf "%ARCHIVE%" --exclude="%SRC%/%SRC%.tar.gz" --exclude="%SRC%/*.tar.gz" --exclude="%SRC%/*.zip" --exclude="%SRC%/.venv" --exclude="%SRC%/venv" --exclude="%SRC%/**/.venv" --exclude="%SRC%/node_modules" --exclude="%SRC%/**/node_modules" --exclude="%SRC%/*.egg-info" --exclude="%SRC%/**/*.egg-info" --exclude="%SRC%/**/__pycache__" --exclude="%SRC%/**/*.py[cod]" --exclude="%SRC%/.pytest_cache" --exclude="%SRC%/**/.pytest_cache" --exclude="%SRC%/.coverage" --exclude="%SRC%/**/.coverage" --exclude="%SRC%/.hypothesis" --exclude="%SRC%/**/.hypothesis" --exclude="%SRC%/.cache" --exclude="%SRC%/coverage" --exclude="%SRC%/htmlcov" --exclude="%SRC%/htmlcov.zip" --exclude="%SRC%/e2e/reports" --exclude="%SRC%/e2e/data" --exclude="%SRC%/build" --exclude="%SRC%/dist" --exclude="%SRC%/.wheelshim" --exclude="%SRC%/console/dist" --exclude="%SRC%/console/node_modules" --exclude="%SRC%/website/dist" --exclude="%SRC%/website/.vite" --exclude="%SRC%/website/node_modules" --exclude="%SRC%/website/tsconfig.tsbuildinfo" --exclude="%SRC%/src/qwenpaw/console" --exclude="%SRC%/src/qwenpaw/docs" --exclude="%SRC%/**/.creator-dev-runtime" --exclude="%SRC%/.codeartsdoer" --exclude="%SRC%/.aone_copilot" --exclude="%SRC%/.agents" --exclude="%SRC%/.claude" --exclude="%SRC%/.cursor" --exclude="%SRC%/.qoder" --exclude="%SRC%/.kiro" --exclude="%SRC%/.codex" --exclude="%SRC%/.vscode" --exclude="%SRC%/.idea" --exclude="%SRC%/.reme" --exclude="%SRC%/.scratch" --exclude="%SRC%/.worktrees" --exclude="%SRC%/.env" --exclude="%SRC%/.env.*" --exclude="%SRC%/**/.env" --exclude="%SRC%/providers.json" --exclude="%SRC%/envs.json" --exclude="%SRC%/config.json" --exclude="%SRC%/*.db" --exclude="%SRC%/*.rdb" --exclude="%SRC%/logs" --exclude="%SRC%/**/.DS_Store" --exclude="%SRC%/**/Thumbs.db" --exclude="%SRC%/**/Desktop.ini" --exclude="%SRC%/**/*~" --exclude="%SRC%/tmp" --exclude="%SRC%/local-docs" --exclude="%SRC%/docs/reviews" --exclude="%SRC%/AGENTS.md" --exclude="%SRC%/CLAUDE.md" "%SRC%"

if errorlevel 1 (
    echo.
    echo [ERROR] Packing failed.
    popd
    pause
    exit /b 1
)

popd

echo.
echo Finished: "%ARCHIVE%"
pause
