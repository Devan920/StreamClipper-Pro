@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Stream Clipper Pro — Production Build Script
REM  Packages the application into StreamClipperPro.exe using
REM  PyInstaller.
REM ============================================================

cd /d "%~dp0"

echo.
echo  ============================================
echo   Stream Clipper Pro — Build Script
echo  ============================================
echo.

REM --- Check Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo         Download from https://python.org
    if "%~1" neq "--no-pause" pause
    exit /b 1
)

REM --- Close running instances if any ---
taskkill /f /im StreamClipperPro.exe >nul 2>&1
taskkill /f /im ffmpeg.exe >nul 2>&1
taskkill /f /im ffprobe.exe >nul 2>&1
ping 127.0.0.1 -n 2 >nul

REM --- Detect ffmpeg location ---
set FFMPEG_PATH=
set FFPROBE_PATH=

for /f "delims=" %%i in ('where ffmpeg 2^>nul') do set "FFMPEG_PATH=%%i"
if not defined FFMPEG_PATH (
    if exist "%USERPROFILE%\.spotiflac\ffmpeg.exe" (
        set "FFMPEG_PATH=%USERPROFILE%\.spotiflac\ffmpeg.exe"
    ) else if exist "C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffmpeg.exe" (
        set "FFMPEG_PATH=C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffmpeg.exe"
    )
)

for /f "delims=" %%i in ('where ffprobe 2^>nul') do set "FFPROBE_PATH=%%i"
if not defined FFPROBE_PATH (
    if exist "%USERPROFILE%\.spotiflac\ffprobe.exe" (
        set "FFPROBE_PATH=%USERPROFILE%\.spotiflac\ffprobe.exe"
    ) else if exist "C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffprobe.exe" (
        set "FFPROBE_PATH=C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffprobe.exe"
    )
)

if defined FFMPEG_PATH (
    echo [OK] Located ffmpeg : !FFMPEG_PATH!
) else (
    echo [WARNING] ffmpeg not found. App will need ffmpeg on system PATH at runtime.
)

if defined FFPROBE_PATH (
    echo [OK] Located ffprobe: !FFPROBE_PATH!
) else (
    echo [WARNING] ffprobe not found. App will need ffprobe on system PATH at runtime.
)

REM --- Install / verify dependencies ---
echo.
echo [1/4] Verifying dependencies...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies from requirements.txt
    if "%~1" neq "--no-pause" pause
    exit /b 1
)

echo [2/4] Verifying PyInstaller...
python -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller.
    if "%~1" neq "--no-pause" pause
    exit /b 1
)

REM --- Prepare binaries and icon flags ---
set "EXTRA_BINARIES="
if defined FFMPEG_PATH (
    set EXTRA_BINARIES=--add-binary "!FFMPEG_PATH!;."
)
if defined FFPROBE_PATH (
    set EXTRA_BINARIES=!EXTRA_BINARIES! --add-binary "!FFPROBE_PATH!;."
)

set "ICON_FLAG="
if exist "assets\app_icon.ico" (
    set "ICON_FLAG=--icon assets\app_icon.ico"
) else if exist "assets\icon.ico" (
    set "ICON_FLAG=--icon assets\icon.ico"
)

REM --- Build with PyInstaller ---
echo.
echo [3/4] Building StreamClipperPro.exe ...
echo        (Collecting modules and compiling; this may take a few minutes...)
echo.

python -m PyInstaller ^
    --noconfirm ^
    --noconsole ^
    --onedir ^
    --name "StreamClipperPro" ^
    !ICON_FLAG! ^
    --add-data "config.py;." ^
    --add-data "stream_ingestor.py;." ^
    --add-data "hype_detector.py;." ^
    --add-data "transcriber.py;." ^
    --add-data "video_editor.py;." ^
    --add-data "caption_generator.py;." ^
    --add-data "video_renderer.py;." ^
    --add-data "title_generator.py;." ^
    --add-data "face_tracker.py;." ^
    --add-data "vod_processor.py;." ^
    --add-data "assets;assets" ^
    !EXTRA_BINARIES! ^
    --hidden-import "vod_processor" ^
    --hidden-import "title_generator" ^
    --hidden-import "caption_generator" ^
    --hidden-import "video_renderer" ^
    --hidden-import "video_editor" ^
    --hidden-import "yt_dlp" ^
    --hidden-import "yt_dlp.extractor" ^
    --hidden-import "yt_dlp.extractor.lazy_extractors" ^
    --hidden-import "yt_dlp.downloader" ^
    --hidden-import "yt_dlp.postprocessor" ^
    --hidden-import "faster_whisper" ^
    --hidden-import "ctranslate2" ^
    --hidden-import "huggingface_hub" ^
    --hidden-import "tokenizers" ^
    --hidden-import "onnxruntime" ^
    --hidden-import "pytchat" ^
    --hidden-import "numpy" ^
    --hidden-import "cv2" ^
    --hidden-import "face_tracker" ^
    --hidden-import "PySide6" ^
    --hidden-import "PySide6.QtCore" ^
    --hidden-import "PySide6.QtGui" ^
    --hidden-import "PySide6.QtWidgets" ^
    --collect-all "faster_whisper" ^
    --collect-all "ctranslate2" ^
    --collect-all "tokenizers" ^
    --collect-all "av" ^
    --collect-all "yt_dlp" ^
    --collect-all "cv2" ^
    main_app.py

if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed!
    echo         Check the output above for details.
    if "%~1" neq "--no-pause" pause
    exit /b 1
)

REM --- Create runtime directories and copy binaries if available ---
echo.
echo [4/4] Finalizing bundle environment...
if not exist "dist\StreamClipperPro\output" mkdir "dist\StreamClipperPro\output"
if not exist "dist\StreamClipperPro\segments" mkdir "dist\StreamClipperPro\segments"
if not exist "dist\StreamClipperPro\assets" xcopy /e /i /y "assets" "dist\StreamClipperPro\assets" >nul 2>&1

if defined FFMPEG_PATH (
    if not exist "dist\StreamClipperPro\ffmpeg.exe" (
        copy /y "!FFMPEG_PATH!" "dist\StreamClipperPro\ffmpeg.exe" >nul 2>&1
    )
)
if defined FFPROBE_PATH (
    if not exist "dist\StreamClipperPro\ffprobe.exe" (
        copy /y "!FFPROBE_PATH!" "dist\StreamClipperPro\ffprobe.exe" >nul 2>&1
    )
)

echo.
echo  ============================================
echo   BUILD COMPLETE!
echo  ============================================
echo.
echo   Executable:  dist\StreamClipperPro\StreamClipperPro.exe
echo.
echo   To launch:
echo     dist\StreamClipperPro\StreamClipperPro.exe
echo.
if "%~1" neq "--no-pause" pause
exit /b 0
