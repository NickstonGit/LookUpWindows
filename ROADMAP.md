# LookUp Windows — Roadmap

## Цель

Лёгкая Windows-утилита для постоянного live-наблюдения за несколькими окнами (1С, RDP, консоль, браузер, мониторинг) через DWM Thumbnail, без тяжёлого GUI-фреймворка и без постоянного screenshot-потока.

## Архитектура

Исходники в `src/`:

- `src/app.py` — application state, native cards/control, Tk dialogs, profiles/hotkeys.
- `src/winapi.py` — enumeration/matching/focus/multi-monitor + asynchronous capture-based change detector.
- `src/dwm.py` — live DWM Thumbnail lifecycle.
- `src/winui.py` — Win32 windows/GDI/input/hotkeys/single-instance helpers.
- `src/trayicon.py` — Explorer tray integration.
- `src/config.py` — versioned model, migration/recovery, portable/local config, autostart.

Ключевой принцип: **DWM — live-preview path; `PrintWindow` — только optional detector path и никогда не UI thread**.

## Следующий шаг перед новыми функциями

Пройти Windows runtime-проверки на Windows 10/11:
- 1С + RDP + браузер;
- два монитора с разным DPI;
- Win+D;
- Explorer restart;
- hotkey/click-through recovery;
- relative crop при resize source;
- 8–12 targets с detector 30+ минут;
- build/autostart/update.

## Backlog после runtime-стабилизации

1. **Visual drag-select crop** поверх preview — только после проверки compositor/input поведения на Windows.
2. **Per-profile quick hotkeys** — если profiles реально используются часто.
3. **Export/import profiles/config** — полезнее, чем усложнять автоматическое group switching.
4. **Optional position lock** — если edge snap недостаточен.
5. **Freeze/snapshot** — только как отдельный bitmap-render pipeline, не смешивать с DWM live path.

Не планируется без отдельного обоснования:
- click forwarding в source-window;
- автоматическое повышение прав;
- тяжёлый переход на Qt/PySide;
- постоянный full-resolution screenshot loop.
