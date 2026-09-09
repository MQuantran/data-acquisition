# -*- mode: python ; coding: utf-8 -*-
# One-FOLDER, windowed build of the data_acquisition plot digitizer.
# Build:  python build.py            (preferred -- also zips it for handout)
#    or:  pyinstaller --noconfirm --clean DataAcquisition.spec
#
# Notes for lab distribution:
#  * one-folder (not one-file): faster start, fewer antivirus false positives,
#    users can see it is "just a folder of files".
#  * UPX disabled on purpose -- UPX-packed exes are a common AV trigger on
#    managed Windows machines.
#  * scipy excluded -- opencv (bundled) is the guaranteed point-detection path.

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/icon.ico', 'assets')],
    hiddenimports=['PIL._tkinter_finder'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'pandas', 'IPython', 'pytest', 'scipy',
              'fitz', 'pymupdf', 'PyQt5', 'PySide2', 'PySide6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DataAcquisition',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
    version='file_version_info.txt',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DataAcquisition',
)
