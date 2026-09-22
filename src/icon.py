from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from pathlib import Path

from winui import gdi32, kernel32, rgb, user32

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


def _draw(size: int) -> bytes:
    dc = gdi32.CreateCompatibleDC(None)
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = size
    header.biHeight = -size
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    if not bitmap or not bits.value:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        raise OSError("CreateDIBSection failed")
    raw = b""
    try:
        old = gdi32.SelectObject(dc, bitmap)
        margin = max(1, size // 10)
        border = max(1, size // 16)
        user32.FillRect(dc, ctypes.byref(wintypes.RECT(0, 0, size, size)), _brush(ICON_BG))
        user32.FrameRect(dc, ctypes.byref(wintypes.RECT(margin, margin, size - margin, size - margin)), _brush(ACCENT))
        inner = margin + border * 2
        gap = max(1, size // 24)
        bar_h = max(1, (size - 2 * inner - 2 * gap) // 3)
        colors = (ACCENT, ACCENT_LIGHT, ACCENT_GRAY)
        y = inner
        for color in colors:
            user32.FillRect(dc, ctypes.byref(wintypes.RECT(inner, y, size - inner, y + bar_h)), _brush(color))
            y += bar_h + gap
        gdi32.SelectObject(dc, old)
        raw_bytes = bytearray(ctypes.string_at(bits.value, size * size * 4))
        # Classic GDI drawing does not populate the alpha channel of a 32-bit DIB.
        # Windows 10/11 and modern icon readers do honour that channel, so an
        # all-zero alpha makes an otherwise valid icon fully transparent.
        for offset in range(3, len(raw_bytes), 4):
            raw_bytes[offset] = 255
        raw = bytes(raw_bytes)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
    return bytes(raw)


_brush_cache: dict[int, int] = {}


def _brush(color: int) -> int:
    cached = _brush_cache.get(color)
    if cached is not None:
        return cached
    handle = int(gdi32.CreateSolidBrush(color) or 0)
    if handle:
        _brush_cache[color] = handle
    return handle


def _downscale(src: bytes, src_size: int, dst_size: int) -> bytes:
    factor = src_size // dst_size
    out = bytearray(dst_size * dst_size * 4)
    for y in range(dst_size):
        for x in range(dst_size):
            r_total = g_total = b_total = a_total = 0
            for dy in range(factor):
                for dx in range(factor):
                    off = ((y * factor + dy) * src_size + (x * factor + dx)) * 4
                    b_total += src[off]
                    g_total += src[off + 1]
                    r_total += src[off + 2]
                    a_total += src[off + 3]
            count = factor * factor
            off = (y * dst_size + x) * 4
            out[off] = b_total // count
            out[off + 1] = g_total // count
            out[off + 2] = r_total // count
            out[off + 3] = a_total // count
    return bytes(out)


def draw_icon_pixels(size: int) -> bytes:
    return _draw(size)


def make_ico_file() -> bytes:
    return _compose_ico({size: _draw(size) for size in (16, 24, 32, 48, 64, 128, 256)})


def _compose_ico(images: dict[int, bytes]) -> bytes:
    sizes = sorted(images)
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        data = _ico_image(size, images[size])
        dir_size = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dir_size, dir_size, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for size in sizes:
        out += _ico_image(size, images[size])
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
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = size
    header.biHeight = -size
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    dc = gdi32.CreateCompatibleDC(None)
    color_bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    if not color_bitmap or not bits.value:
        raise OSError("icon bitmap creation failed")
    try:
        pixels = _draw(size)
        ctypes.memmove(bits.value, pixels, len(pixels))
        mask_stride = ((size + 31) // 32) * 4
        mask_bits = ctypes.create_string_buffer(mask_stride * size)
        mask = gdi32.CreateBitmap(size, size, 1, 1, ctypes.cast(mask_bits, ctypes.c_void_p))
        if not mask:
            raise OSError("icon mask creation failed")
        info = ICONINFO()
        info.fIcon = True
        info.hbmMask = mask
        info.hbmColor = color_bitmap
        hicon = user32.CreateIconIndirect(ctypes.byref(info))
        gdi32.DeleteObject(mask)
        if not hicon:
            raise OSError("CreateIconIndirect failed")
        return int(hicon)
    finally:
        gdi32.DeleteObject(color_bitmap)
        gdi32.DeleteDC(dc)


def destroy_tray_icon(hicon: int) -> None:
    if hicon:
        user32.DestroyIcon(hicon)