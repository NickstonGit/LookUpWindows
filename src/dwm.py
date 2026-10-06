from __future__ import annotations

import ctypes
import logging
import weakref
from ctypes import wintypes

logger = logging.getLogger(__name__)

dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

HWND = ctypes.c_void_p
HTHUMBNAIL = ctypes.c_void_p

# Values from the Windows SDK (dwmapi.h / dwmapi.ntdef.h).  They are not a
# sequential bit enumeration: shipping the wrong values silently swapped the
# meaning of every field in THUMBNAIL_PROPERTIES.
DWM_TNP_RECTDESTINATION = 0x00000001
DWM_TNP_RECTSOURCE = 0x00000002
DWM_TNP_OPACITY = 0x00000004
DWM_TNP_VISIBLE = 0x00000008
DWM_TNP_SOURCECLIENTAREAONLY = 0x00000010


class THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", wintypes.RECT),
        ("rcSource", wintypes.RECT),
        ("opacity", wintypes.BYTE),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    ]


dwmapi.DwmRegisterThumbnail.argtypes = [HWND, HWND, ctypes.POINTER(HTHUMBNAIL)]
dwmapi.DwmRegisterThumbnail.restype = ctypes.c_long
dwmapi.DwmUnregisterThumbnail.argtypes = [HTHUMBNAIL]
dwmapi.DwmUnregisterThumbnail.restype = ctypes.c_long
dwmapi.DwmUpdateThumbnailProperties.argtypes = [HTHUMBNAIL, ctypes.POINTER(THUMBNAIL_PROPERTIES)]
dwmapi.DwmUpdateThumbnailProperties.restype = ctypes.c_long
dwmapi.DwmQueryThumbnailSourceSize.argtypes = [HTHUMBNAIL, ctypes.POINTER(wintypes.SIZE)]
dwmapi.DwmQueryThumbnailSourceSize.restype = ctypes.c_long


def _unregister_thumbnail(handle_value: int) -> None:
    if not handle_value:
        return
    hr = dwmapi.DwmUnregisterThumbnail(HTHUMBNAIL(handle_value))
    if hr != 0:
        logger.debug(
            "DwmUnregisterThumbnail failed during cleanup: HRESULT 0x%08X",
            hr & 0xFFFFFFFF,
        )


class Thumbnail:
    def __init__(self, dest_hwnd: int, source_hwnd: int):
        handle = HTHUMBNAIL()
        hr = dwmapi.DwmRegisterThumbnail(dest_hwnd, source_hwnd, ctypes.byref(handle))
        if hr != 0 or not handle.value:
            raise OSError(f"DwmRegisterThumbnail failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
        self.handle = handle
        self._finalizer = weakref.finalize(self, _unregister_thumbnail, int(handle.value))
        self._visible = True
        self._dest_rect = (0, 0, 0, 0)
        self._source_rect: tuple[int, int, int, int] | None = None
        self._opacity = 255
        self._client_area_only = False
        self._last_applied: tuple[object, ...] | None = None

    def source_size(self) -> tuple[int, int]:
        size = wintypes.SIZE()
        hr = dwmapi.DwmQueryThumbnailSourceSize(self.handle, ctypes.byref(size))
        if hr != 0:
            logger.debug(
                "DwmQueryThumbnailSourceSize failed: HRESULT 0x%08X",
                hr & 0xFFFFFFFF,
            )
            return (0, 0)
        return (int(size.cx), int(size.cy))

    def update(
        self,
        dest_rect: tuple[int, int, int, int] | None = None,
        source_rect: tuple[int, int, int, int] | bool | None = None,
        visible: bool | None = None,
        opacity: int | None = None,
        client_area_only: bool = False,
    ) -> bool:
        if dest_rect is not None:
            self._dest_rect = tuple(dest_rect)
        if source_rect is False:
            self._source_rect = None
        elif source_rect is not None:
            self._source_rect = tuple(source_rect)
        if visible is not None:
            self._visible = bool(visible)
        if opacity is not None:
            self._opacity = max(0, min(255, int(opacity)))
        self._client_area_only = bool(client_area_only)

        state = (
            self._dest_rect,
            self._source_rect,
            self._visible,
            self._opacity,
            self._client_area_only,
        )
        if state == self._last_applied:
            return True

        props = THUMBNAIL_PROPERTIES()
        props.dwFlags = (
            DWM_TNP_VISIBLE
            | DWM_TNP_OPACITY
            | DWM_TNP_RECTDESTINATION
            | DWM_TNP_SOURCECLIENTAREAONLY
        )
        props.rcDestination = wintypes.RECT(*self._dest_rect)
        props.opacity = self._opacity
        props.fVisible = self._visible
        props.fSourceClientAreaOnly = self._client_area_only
        if self._source_rect is not None:
            props.dwFlags |= DWM_TNP_RECTSOURCE
            props.rcSource = wintypes.RECT(*self._source_rect)
        hr = dwmapi.DwmUpdateThumbnailProperties(self.handle, ctypes.byref(props))
        ok = hr == 0
        if ok:
            self._last_applied = state
        else:
            logger.debug(
                "DwmUpdateThumbnailProperties failed: HRESULT 0x%08X",
                hr & 0xFFFFFFFF,
            )
        return ok

    def close(self) -> None:
        if self._finalizer.alive:
            self._finalizer()
        self.handle = HTHUMBNAIL()
        self._last_applied = None

    def __enter__(self) -> "Thumbnail":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
