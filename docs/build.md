# Сборка и проверка артефакта

Документ описывает технические детали production-сборки, которые неуместно держать
в основном README, но полезно знать при диагностике.

## Подтверждённый toolchain

```text
CPython 3.14.x x64
PyInstaller 6.22.3
onefile EXE
```

Сборка у проекта ровно одна, и она же production:

```bat
build-onefile.bat
```

Единственный production-артефакт:

```text
dist/LookUpWindows.exe
```

Варианта `onedir` нет и не планируется: он не является ни production-, ни
release-артефактом, и в релиз не публикуется.

`tools/check_build_env.py` содержит эти значения в единственном месте
(`RELEASE_PYTHON`, `RELEASE_ARCHITECTURE`, `RELEASE_PYINSTALLER`).
`build-onefile.bat` вызывает его с `--mode release` и останавливается при любом
несовпадении: Python 3.10, 3.11, 3.12, 3.13, 3.15+ и 32-битный интерпретатор
отклоняются одинаково. Сборка на другом интерпретаторе даёт артефакт, который
никто не проверял.

Исходники при этом совместимы с Python 3.10+ — это исключительно удобство
разработки. Матрица совместимости в `.github/workflows/windows-ci.yml` проверяет
только импорт, компиляцию и тесты и ничего не собирает; релиз всегда собирается
на 3.14.

## Почему именно PyInstaller 6.22.3

В Python 3.14 данные Tcl/Tk (Tcl/Tk 9.0.4) поставляются не отдельными файлами,
а архивами, которые интерпретатор монтирует через zipfs. Ранние версии
PyInstaller не умели включать такой ресурс в frozen-приложение.

Симптом проявляется **после** успешной сборки, при первом же создании `Tk`:

```text
_tkinter.TclError: Cannot find a usable init.tcl
```

Сборка при этом завершается с кодом 0, поэтому сам по себе exit-код PyInstaller
не является признаком готовности артефакта.

## Проверка готовности

Готовый артефакт обязательно прогоняется через runtime smoke до публикации:

```powershell
python tools/verify_frozen_runtime.py --exe dist\LookUpWindows.exe
python tools/runtime_smoke.py --mode exe --exe dist\LookUpWindows.exe --all
```

`tools/verify_frozen_runtime.py` читает архив PyInstaller внутри собранного EXE и
требует именно `python314.dll` вместе с Tcl/Tk-данными zipfs: зелёная сборка
сама по себе не доказывает, какой интерпретатор попал в артефакт.

Полный набор сценариев — это реестр `SCENARIOS` в `tools/runtime_smoke.py`; он
запускается целиком в `.github/workflows/release.yml` на том же
`dist/LookUpWindows.exe` перед публикацией; при ошибке любого сценария публикация
не выполняется. Список здесь намеренно не дублируется: он меняется вместе с
кодом, а копия в документации устаревает первой.

Состав публикации фиксирован: onefile EXE, его SHA-256, архив исходников и его
SHA-256, а также `release-manifest.json` с fingerprint исходников и результатами гейтов.
Сборка инвалидирует старый manifest и sidecar. После успешного прохождения всех
гейтов записать их результаты в `build/release-gates.json`, затем выполнить:

```powershell
python tools/release_manifest.py --record-gates --gates build/release-gates.json
python tools/release_manifest.py --write --gates build/release-gates.json
python tools/release_manifest.py --verify
```

Проверка выполняется повторно непосредственно перед публикацией. Изменение байтов
EXE/ZIP или исходников после гейтов делает подтверждение недействительным.

## Если виден `Cannot find a usable init.tcl`

1. Проверить, что сборка выполнена ровно на CPython 3.14 x64:
   `python tools\check_build_env.py --mode release`.
2. Проверить версию PyInstaller: должна быть ровно 6.22.3
   (`python -c "import PyInstaller; print(PyInstaller.__version__)"`).
3. Пересобрать артефакт и прогнать smoke-сценарий `responsive`.

Запуск из исходников (`python src\app.py`) этой ошибки не имеет: там
используется обычный Tcl/Tk из установленного Python.
