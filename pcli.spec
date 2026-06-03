# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import copy_metadata, collect_data_files, collect_submodules

datas = []
datas += copy_metadata('pcli')
datas += collect_data_files('magika')  # magika ML model files required at runtime

# markitdown PDF converter uses pdfminer and pdfplumber — PyInstaller won't
# auto-detect these because they are imported lazily inside the converter class.
hidden_imports = (
    collect_submodules('pdfminer') +
    collect_submodules('pdfplumber')
)


a = Analysis(
    ['src\\pcli\\cli.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['yaml'] + hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pcli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='pcli',
)
