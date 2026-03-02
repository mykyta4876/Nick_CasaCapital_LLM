@echo off
echo ============================================
echo MCA OCR Worker - Setup Script
echo ============================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.11+ from https://python.org
    pause
    exit /b 1
)

echo [1/6] Creating virtual environment...
python -m venv venv
if %errorlevel% neq 0 (
    echo ERROR: Failed to create virtual environment
    pause
    exit /b 1
)

echo [2/6] Activating virtual environment...
call venv\Scripts\activate.bat

echo [3/6] Upgrading pip...
python -m pip install --upgrade pip

echo [4/6] Installing core dependencies...
pip install pdfplumber pymupdf pillow openpyxl

echo [5/6] Installing PyTorch with CUDA support...
echo This may take a few minutes (downloading ~3GB)...
pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128

echo [6/6] Installing Surya OCR...
pip install surya-ocr

echo.
echo ============================================
echo Setup Complete!
echo ============================================
echo.
echo To activate the environment, run:
echo     .\venv\Scripts\activate
echo.
echo To test the installation, run:
echo     python src\test_setup.py
echo.
echo To process bank statements:
echo     python src\moneythumb_extractor.py samples\your_file.csv output\
echo.
echo To run underwriting:
echo     python src\underwriting_engine.py output\analysis.json
echo.
pause
