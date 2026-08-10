from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("ruida_re")
datas = collect_data_files("ruida_re")
