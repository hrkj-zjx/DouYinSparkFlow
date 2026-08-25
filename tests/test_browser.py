"""浏览器启动层的纯 fake 单元测试，不启动真实 Playwright/Chromium。"""

import sys
import unittest
from unittest.mock import patch

import core.browser as browser_module


class FakeChromium:
    """记录 headless 参数，或按需模拟 Chromium 启动失败。"""

    def __init__(self, launch_error=None):
        self.launch_error = launch_error
        self.headless = None

    def launch(self, headless):
        self.headless = headless
        if self.launch_error is not None:
            raise self.launch_error
        return "fake-browser"


class FakePlaywright:
    """提供最小 chromium 属性，并记录 stop 是否执行。"""

    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


class BrowserLifecycleTests(unittest.TestCase):
    def test_headless_mode_comes_from_runtime_config(self):
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)

        with patch.object(
            browser_module, "_start_playwright", return_value=playwright
        ), patch.object(browser_module, "_configure_bundled_browser_path"):
            returned_playwright, returned_browser = browser_module.get_browser(
                {"browserHeadless": False}
            )

        self.assertIs(returned_playwright, playwright)
        self.assertEqual(returned_browser, "fake-browser")
        self.assertFalse(chromium.headless)
        self.assertFalse(playwright.stopped)

    def test_launch_failure_stops_partially_started_playwright(self):
        chromium = FakeChromium(RuntimeError("launch failed"))
        playwright = FakePlaywright(chromium)

        with patch.object(
            browser_module, "_start_playwright", return_value=playwright
        ), patch.object(browser_module, "_configure_bundled_browser_path"):
            with self.assertRaisesRegex(
                browser_module.BrowserStartupError, "Chromium 启动失败"
            ):
                browser_module.get_browser({"browserHeadless": True})

        self.assertTrue(playwright.stopped)

    def test_missing_executable_does_not_trigger_implicit_download(self):
        chromium = FakeChromium(RuntimeError("Executable doesn't exist"))
        playwright = FakePlaywright(chromium)

        with patch.object(
            browser_module, "_start_playwright", return_value=playwright
        ), patch.object(browser_module, "_configure_bundled_browser_path"), patch.object(
            browser_module, "install_browser"
        ) as install_browser:
            with self.assertRaisesRegex(
                browser_module.BrowserStartupError, "playwright install chromium"
            ):
                browser_module.get_browser({"browserHeadless": True})

        install_browser.assert_not_called()
        self.assertTrue(playwright.stopped)

    def test_explicit_install_uses_current_python_environment(self):
        with patch.object(browser_module, "_configure_bundled_browser_path"), patch.object(
            browser_module.subprocess, "run"
        ) as subprocess_run:
            browser_module.install_browser()

        subprocess_run.assert_called_once_with(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
