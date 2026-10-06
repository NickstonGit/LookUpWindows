"""DWM_THUMBNAIL_PROPERTIES flag values must match the Windows SDK."""

import ctypes
import unittest

try:  # dwm.py binds dwmapi at import time, so it only imports on Windows.
    import dwm
except (ImportError, OSError, AttributeError):  # pragma: no cover - non-Windows
    dwm = None


@unittest.skipIf(dwm is None, "DWM module requires Windows")
class DwmFlagValueTests(unittest.TestCase):
    def test_flag_values_match_the_sdk(self):
        # Values from the Windows SDK; shipping the wrong ones silently swapped
        # the meaning of every field of THUMBNAIL_PROPERTIES.
        self.assertEqual(dwm.DWM_TNP_RECTDESTINATION, 0x00000001)
        self.assertEqual(dwm.DWM_TNP_RECTSOURCE, 0x00000002)
        self.assertEqual(dwm.DWM_TNP_OPACITY, 0x00000004)
        self.assertEqual(dwm.DWM_TNP_VISIBLE, 0x00000008)
        self.assertEqual(dwm.DWM_TNP_SOURCECLIENTAREAONLY, 0x00000010)

    def test_flags_are_distinct_bits(self):
        flags = [
            dwm.DWM_TNP_RECTDESTINATION,
            dwm.DWM_TNP_RECTSOURCE,
            dwm.DWM_TNP_OPACITY,
            dwm.DWM_TNP_VISIBLE,
            dwm.DWM_TNP_SOURCECLIENTAREAONLY,
        ]
        self.assertEqual(len(set(flags)), len(flags))
        combined = 0
        for flag in flags:
            self.assertEqual(combined & flag, 0)
            combined |= flag
        self.assertEqual(combined, 0x1F)

    def test_removed_misleading_alias_is_gone(self):
        self.assertFalse(hasattr(dwm, "DWM_TNP_VISIBLEBITS"))


@unittest.skipIf(dwm is None, "DWM module requires Windows")
class ThumbnailPropertyMaskTests(unittest.TestCase):
    """The mask handed to DwmUpdateThumbnailProperties is checked, not the call."""

    def _thumbnail(self):
        thumb = object.__new__(dwm.Thumbnail)
        thumb.handle = dwm.HTHUMBNAIL(1)
        thumb._finalizer = None
        thumb._visible = True
        thumb._dest_rect = (0, 0, 0, 0)
        thumb._source_rect = None
        thumb._opacity = 255
        thumb._client_area_only = False
        thumb._last_applied = None
        return thumb

    def _capture_update(self, thumb, **kwargs):
        captured = {}

        def fake_update(handle, props):
            # The caller passes ctypes.byref(props); copy the structure out.
            size = ctypes.sizeof(dwm.THUMBNAIL_PROPERTIES)
            captured["props"] = dwm.THUMBNAIL_PROPERTIES.from_buffer_copy(
                ctypes.string_at(props, size)
            )
            return 0

        original = dwm.dwmapi.DwmUpdateThumbnailProperties
        dwm.dwmapi.DwmUpdateThumbnailProperties = fake_update
        try:
            self.assertTrue(thumb.update(**kwargs))
        finally:
            dwm.dwmapi.DwmUpdateThumbnailProperties = original
        return captured["props"]

    def test_visibility_is_always_applied(self):
        # Without DWM_TNP_VISIBLE a hidden thumbnail would silently stay visible.
        thumb = self._thumbnail()
        props = self._capture_update(thumb, dest_rect=(0, 0, 10, 10), visible=False)
        self.assertTrue(props.dwFlags & dwm.DWM_TNP_VISIBLE)
        self.assertTrue(props.dwFlags & dwm.DWM_TNP_RECTDESTINATION)
        self.assertTrue(props.dwFlags & dwm.DWM_TNP_OPACITY)
        self.assertEqual(props.fVisible, 0)
        self.assertEqual(
            (
                props.rcDestination.left,
                props.rcDestination.top,
                props.rcDestination.right,
                props.rcDestination.bottom,
            ),
            (0, 0, 10, 10),
        )

    def test_source_rect_flag_only_with_explicit_source_rect(self):
        thumb = self._thumbnail()
        props = self._capture_update(thumb, dest_rect=(0, 0, 10, 10))
        self.assertFalse(props.dwFlags & dwm.DWM_TNP_RECTSOURCE)

        thumb = self._thumbnail()
        props = self._capture_update(
            thumb, dest_rect=(0, 0, 10, 10), source_rect=(1, 2, 3, 4)
        )
        self.assertTrue(props.dwFlags & dwm.DWM_TNP_RECTSOURCE)
        self.assertEqual(props.rcSource.left, 1)
        self.assertEqual(props.rcSource.bottom, 4)

    def test_all_fields_supplied_yield_the_full_mask(self):
        thumb = self._thumbnail()
        props = self._capture_update(
            thumb,
            dest_rect=(0, 0, 10, 10),
            source_rect=(0, 0, 10, 10),
            visible=True,
            opacity=128,
            client_area_only=True,
        )
        self.assertEqual(props.dwFlags, 0x1F)
        self.assertEqual(props.opacity, 128)
        self.assertEqual(props.fSourceClientAreaOnly, 1)

    def test_client_area_only_follows_the_requested_value(self):
        thumb = self._thumbnail()
        props = self._capture_update(thumb, dest_rect=(0, 0, 4, 4), client_area_only=True)
        self.assertTrue(props.dwFlags & dwm.DWM_TNP_SOURCECLIENTAREAONLY)
        self.assertEqual(props.fSourceClientAreaOnly, 1)


if __name__ == "__main__":
    unittest.main()