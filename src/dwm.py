from __future__ import annotations

import ctypes
from ctypes import wintypes

dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

HWND = ctypes.c_void_p
HTHUMBNAIL = ctypes.c_void_p

DWM_TNP_VISIBLEBITS = 0x01
DWM_TNP_OPACITY = 0x02
DWM_TNP_RECTDESTINATION = 0x04
DWM_TNP_RECTSOURCE = 0x08
DWM_TNP_SOURCECLIENTAREAONLY = 0x10


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


class Thumbnail:
    def __init__(self, dest_hwnd: int, source_hwnd: int):
        handle = HTHUMBNAIL()
        hr = dwmapi.DwmRegisterThumbnail(dest_hwnd, source_hwnd, ctypes.byref(handle))
        if hr != 0 or not handle.value:
            raise OSError(f"DwmRegisterThumbnail failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
        self.handle = handle
        self._visible = True
        self._dest_rect = (0, 0, 0, 0)
        self._source_rect: tuple[int, int, int, int] | None = None
        self._opacity = 255

    def source_size(self) -> tuple[int, int]:
        size = wintypes.SIZE()
        if dwmapi.DwmQueryThumbnailSourceSize(self.handle, ctypes.byref(size)) != 0:
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

        props = THUMBNAIL_PROPERTIES()
        props.dwFlags = (
            DWM_TNP_VISIBLEBITS
            | DWM_TNP_OPACITY
            | DWM_TNP_RECTDESTINATION
            | DWM_TNP_SOURCECLIENTAREAONLY
        )
        props.rcDestination = wintypes.RECT(*self._dest_rect)
        props.opacity = self._opacity
        props.fVisible = self._visible
        props.fSourceClientAreaOnly = bool(client_area_only)
        if self._source_rect is not None:
            props.dwFlags |= DWM_TNP_RECTSOURCE
            props.rcSource = wintypes.RECT(*self._source_rect)
        return dwmapi.DwmUpdateThumbnailProperties(self.handle, ctypes.byref(props)) == 0

    def close(self) -> None:
        if self.handle is not None and self.handle.value:
            dwmapi.DwmUnregisterThumbnail(self.handle)
        self.handle = HTHUMBNAIL()

    def __enter__(self) -> "Thumbnail":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
