@echo off
chcp 65001 >nul
setlocal EnableExtensions

echo ================================================
echo   Сборка DICOM Viewer в автономный EXE
echo ================================================
echo.

cd /d "%~dp0"
echo [i] Рабочая папка: %CD%
echo.

REM --- Проверка Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo [X] Python не найден в PATH.
    pause
    exit /b 1
)

REM --- Проверка PyInstaller ---
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [!] PyInstaller не установлен. Устанавливаю...
    pip install pyinstaller
    if errorlevel 1 (
        echo [X] Не удалось установить PyInstaller.
        pause
        exit /b 1
    )
)

REM --- Проверка pynetdicom ---
python -c "import pynetdicom" >nul 2>&1
if errorlevel 1 (
    echo [!] pynetdicom не установлен. Устанавливаю...
    pip install pynetdicom
    if errorlevel 1 (
        echo [X] Не удалось установить pynetdicom.
        pause
        exit /b 1
    )
)

REM --- Убиваем запущенный EXE ---
tasklist /FI "IMAGENAME eq DICOM_Viewer.exe" 2>NUL | find /I "DICOM_Viewer.exe" >NUL
if not errorlevel 1 (
    echo [!] DICOM_Viewer.exe запущен. Закрываю...
    taskkill /F /IM "DICOM_Viewer.exe" >nul 2>&1
    timeout /t 2 /nobreak >nul
)

REM --- Проверка bundle ---
if not exist "bundle\mdb_java\jackcess-4.0.8.jar" (
    echo [X] Не найдена папка bundle\mdb_java\
    pause
    exit /b 1
)

if not exist "viewer.py" (
    echo [X] Не найден файл viewer.py
    pause
    exit /b 1
)

REM --- Иконка ---
set "ICON_ARG="
if exist "icon.ico" (
    set "ICON_ARG=--icon=icon.ico"
) else (
    echo [!] Файл icon.ico не найден, сборка без иконки.
)

REM --- Очистка ---
echo [i] Очистка build, dist, *.spec...
if exist "build" rmdir /s /q "build" >nul 2>&1
if exist "dist"  rmdir /s /q "dist"  >nul 2>&1
if exist "DICOM_Viewer.spec" del /q "DICOM_Viewer.spec" >nul 2>&1

echo.
echo [*] Запуск PyInstaller (2-5 минут)...
echo.

python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "DICOM_Viewer" ^
    %ICON_ARG% ^
    --add-data "bundle;bundle" ^
    --add-data "icon.ico;." ^
    --hidden-import=pydicom ^
    --hidden-import=access_parser ^
    --hidden-import=win32com ^
    --hidden-import=win32com.client ^
    --hidden-import=pythoncom ^
    --hidden-import=pywintypes ^
    --hidden-import=win32timezone ^
    --hidden-import=mdb_sync ^
    --hidden-import=numpy ^
    --hidden-import=numpy.core ^
    --hidden-import=numpy.core._multiarray_umath ^
    --hidden-import=pynetdicom ^
    --hidden-import=pynetdicom._globals ^
    --hidden-import=pynetdicom._handlers ^
    --hidden-import=pynetdicom.presentation ^
    --hidden-import=pynetdicom.sop_class ^
    --hidden-import=pynetdicom.pdu_primitives ^
    --hidden-import=pynetdicom.dimse_messages ^
    --hidden-import=pynetdicom.dul ^
    --hidden-import=pynetdicom.events ^
    --hidden-import=pynetdicom.fsm ^
    --hidden-import=pynetdicom.acse ^
    --hidden-import=pynetdicom.dimse ^
    --hidden-import=pynetdicom.presentation ^
    --hidden-import=pynetdicom.service_class ^
    --hidden-import=structlog ^
    --hidden-import=requests ^
    --hidden-import=PyQt5.QtCore ^
    --hidden-import=PyQt5.QtGui ^
    --hidden-import=PyQt5.QtWidgets ^
    --hidden-import=PyQt5.QtPrintSupport ^
    --collect-data=pydicom ^
    --collect-data=access_parser ^
    --collect-submodules=numpy ^
    --collect-submodules=pynetdicom ^
    --collect-all=structlog ^
    --exclude-module=tkinter ^
    --exclude-module=matplotlib ^
    --exclude-module=scipy ^
    --exclude-module=PIL ^
    --exclude-module=PyQt5.QtWebEngineWidgets ^
    --exclude-module=PyQt5.QtMultimedia ^
    --exclude-module=PyQt5.QtQml ^
    --exclude-module=PyQt5.QtQuick ^
    --exclude-module=PyQt5.QtNetwork ^
    --exclude-module=PyQt5.QtWebEngineCore ^
    --exclude-module=PyQt5.QtWebEngine ^
    --exclude-module=PyQt5.QtWebSockets ^
    --exclude-module=PyQt5.QtXml ^
    --exclude-module=PyQt5.QtSvg ^
    --exclude-module=PyQt5.QtSql ^
    --exclude-module=PyQt5.QtTest ^
    --exclude-module=PyQt5.QtBluetooth ^
    --exclude-module=PyQt5.QtNfc ^
    --exclude-module=PyQt5.QtPositioning ^
    --exclude-module=PyQt5.QtLocation ^
    --exclude-module=PyQt5.QtSerialPort ^
    viewer.py

set "BUILD_RESULT=%ERRORLEVEL%"

if not "%BUILD_RESULT%"=="0" (
    echo.
    echo [X] Ошибка сборки, код: %BUILD_RESULT%
    pause
    exit /b 1
)

if not exist "dist\DICOM_Viewer.exe" (
    echo [X] EXE не создан.
    pause
    exit /b 1
)

REM --- MSI Java ---
if exist "OpenJDK8U-jdk_x64_windows_hotspot_8u504b01.msi" (
    copy /Y "OpenJDK8U-jdk_x64_windows_hotspot_8u504b01.msi" "dist\" >nul 2>&1
    echo [i] OpenJDK MSI скопирован в dist\
)

echo.
echo ================================================
echo   ГОТОВО!
echo   EXE: dist\DICOM_Viewer.exe
echo ================================================
echo.

start "" explorer "dist"
pause
endlocal