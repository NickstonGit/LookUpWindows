import unittest

from windowmatch import matches_target, preferred_candidate_index


class MatchesTargetTests(unittest.TestCase):
    class Entry:
        def __init__(self, process="", title_contains=""):
            self.process = process
            self.title_contains = title_contains

    def test_process_only_entry_matches_every_window_of_that_process(self):
        entry = self.Entry(process="Code.exe")
        self.assertTrue(matches_target(entry, "Code.exe", "myproj - Visual Studio Code"))
        self.assertTrue(matches_target(entry, "code.exe", "otherproj - Visual Studio Code"))
        self.assertFalse(matches_target(entry, "devenv.exe", "myproj"))

    def test_title_filter_narrows_the_process(self):
        entry = self.Entry(process="Code.exe", title_contains="myproj")
        self.assertTrue(matches_target(entry, "Code.exe", "myproj - Visual Studio Code"))
        self.assertFalse(matches_target(entry, "Code.exe", "otherproj - Visual Studio Code"))

    def test_empty_entry_matches_any_window(self):
        self.assertTrue(matches_target(self.Entry(), "anything.exe", "anything"))


class PreferredCandidateTests(unittest.TestCase):
    def test_old_config_without_hints_keeps_legacy_first_match(self):
        candidates = [("Alpha", "AppWnd"), ("Beta", "AppWnd")]
        self.assertEqual(preferred_candidate_index(candidates), 0)

    def test_last_title_disambiguates_same_process_windows(self):
        candidates = [("Inbox - Mail", "Chrome_WidgetWin_1"), ("ERP - Chrome", "Chrome_WidgetWin_1")]
        self.assertEqual(
            preferred_candidate_index(candidates, title_hint="ERP - Chrome", class_hint="Chrome_WidgetWin_1"),
            1,
        )

    def test_stale_title_hint_is_soft_and_never_hides_all_matches(self):
        candidates = [("New title", "AppWnd"), ("Other title", "AppWnd")]
        self.assertEqual(
            preferred_candidate_index(candidates, title_hint="Old title", class_hint="AppWnd"),
            0,
        )

    def test_unique_class_hint_wins(self):
        candidates = [("Document", "DialogWnd"), ("Document", "MainWnd")]
        self.assertEqual(preferred_candidate_index(candidates, class_hint="MainWnd"), 1)


if __name__ == "__main__":
    unittest.main()
