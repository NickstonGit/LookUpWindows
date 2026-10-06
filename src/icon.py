from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from pathlib import Path

from winui import brush, gdi32, rgb, user32

DIB_RGB_COLORS = 0
BI_RGB = 0

ACCENT = rgb(0x4F, 0x8C, 0xFF)
ACCENT_LIGHT = rgb(0x7A, 0xB8, 0xFF)
ACCENT_GRAY = rgb(0x8A, 0x8B, 0x91)
ICON_BG = rgb(0x1F, 0x20, 0x23)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p),
    ]


gdi32.CreateDIBSection.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFOHEADER),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = ctypes.c_void_p

user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = ctypes.c_void_p
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
user32.DestroyIcon.restype = wintypes.BOOL


def _new_bitmap_header(size: int) -> BITMAPINFOHEADER:
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = size
    header.biHeight = -size
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = BI_RGB
    return header


def _render_bitmap(size: int) -> tuple[int, int]:
    """Render the icon once and return ``(HBITMAP, bits_address)``.

    The selected object is always restored before the memory DC is deleted.  The
    caller owns the returned HBITMAP and may keep it long enough for
    CreateIconIndirect or copy its pixels for ICO serialization.
    """
    dc = gdi32.CreateCompatibleDC(None)
    if not dc:
        raise OSError("icon DC creation failed")
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(
        dc,
        ctypes.byref(_new_bitmap_header(size)),
        DIB_RGB_COLORS,
        ctypes.byref(bits),
        None,
        0,
    )
    if not bitmap or not bits.value:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        raise OSError("CreateDIBSection failed")

    old = gdi32.SelectObject(dc, bitmap)
    if not old:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        raise OSError("SelectObject(icon bitmap) failed")

    try:
        margin = max(1, size // 10)
        border = max(1, size // 16)
        user32.FillRect(dc, ctypes.byref(wintypes.RECT(0, 0, size, size)), brush(ICON_BG))
        user32.FrameRect(
            dc,
            ctypes.byref(wintypes.RECT(margin, margin, size - margin, size - margin)),
            brush(ACCENT),
        )
        inner = margin + border * 2
        gap = max(1, size // 24)
        bar_h = max(1, (size - 2 * inner - 2 * gap) // 3)
        y = inner
        for color in (ACCENT, ACCENT_LIGHT, ACCENT_GRAY):
            user32.FillRect(
                dc,
                ctypes.byref(wintypes.RECT(inner, y, size - inner, y + bar_h)),
                brush(color),
            )
            y += bar_h + gap

        # Classic GDI leaves the alpha channel at zero.  Set it in one strided
        # slice rather than 65k Python loop iterations for a 256px icon.
        raw = bytearray(ctypes.string_at(bits.value, size * size * 4))
        raw[3::4] = b"\xff" * (size * size)
        ctypes.memmove(bits.value, bytes(raw), len(raw))
    except Exception:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        raise
    else:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteDC(dc)
        return (int(bitmap), int(bits.value))


def _draw(size: int) -> bytes:
    bitmap, bits_address = _render_bitmap(size)
    try:
        return ctypes.string_at(bits_address, size * size * 4)
    finally:
        gdi32.DeleteObject(bitmap)


def make_ico_file() -> bytes:
    return _compose_ico({size: _draw(size) for size in (16, 24, 32, 48, 64, 128, 256)})


def _compose_ico(images: dict[int, bytes]) -> bytes:
    sizes = sorted(images)
    payloads = {size: _ico_image(size, images[size]) for size in sizes}
    out = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        data = payloads[size]
        dir_size = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dir_size, dir_size, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for size in sizes:
        out += payloads[size]
    return bytes(out)


def _ico_image(size: int, pixels: bytes) -> bytes:
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, BI_RGB, 0, 0, 0, 0, 0
    )
    stride = size * 4
    bottom_up = b"".join(
        pixels[row * stride:(row + 1) * stride]
        for row in range(size - 1, -1, -1)
    )
    mask_stride = ((size + 31) // 32) * 4
    and_mask = bytes(mask_stride * size)
    return header + bottom_up + and_mask


def ensure_ico_file(path: Path) -> bool:
    try:
        data = make_ico_file()
        if path.exists() and path.read_bytes() == data:
            return True
        path.write_bytes(data)
        return True
    except OSError:
        return False


def make_tray_icon() -> int:
    size = 32
    color_bitmap = 0
    mask = 0
    try:
        # Reuse the same rendered DIB as the ICO path instead of drawing once
        # into a throwaway bitmap and copying into a second DIB.
        color_bitmap, _bits = _render_bitmap(size)
        mask_stride = ((size + 31) // 32) * 4
        mask_bits = ctypes.create_string_buffer(mask_stride * size)
        mask = int(
            gdi32.CreateBitmap(
                size,
                size,
                1,
                1,
                ctypes.cast(mask_bits, ctypes.c_void_p),
            )
            or 0
        )
        if not mask:
            raise OSError("icon mask creation failed")
        info = ICONINFO()
        info.fIcon = True
        info.hbmMask = mask
        info.hbmColor = color_bitmap
        hicon = user32.CreateIconIndirect(ctypes.byref(info))
        if not hicon:
            raise OSError("CreateIconIndirect failed")
        return int(hicon)
    finally:
        if mask:
            gdi32.DeleteObject(mask)
        if color_bitmap:
            gdi32.DeleteObject(color_bitmap)


def destroy_tray_icon(hicon: int) -> None:
    if hicon:
        user32.DestroyIcon(hicon)
