import unittest

from core.preflight import PreflightError, run_preflight


RUNTIME_CONFIG = {
    "browserTimeout": 5000,
    "friendListTimeout": 500,
    "taskRetryTimes": 1,
    "blockBrowserResources": True,
    "browserHeadless": True,
}


class FakePage:
    """只实现导航和等待列表；若预检误触输入 API，测试会因缺少方法立即失败。"""

    def __init__(self, selector_error=None):
        self.selector_error = selector_error
        self.visited_url = None
        self.wait_until = None

    def goto(self, url, wait_until=None):
        self.visited_url = url
        self.wait_until = wait_until

    def wait_for_selector(self, _selector, timeout):
        if self.selector_error is not None:
            raise self.selector_error
        self.timeout = timeout


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.routes = []

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value

    def set_default_timeout(self, value):
        self.default_timeout = value

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def add_cookies(self, cookies):
        self.cookies = cookies

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.closed = False

    def new_context(self):
        return self.context

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class PreflightTests(unittest.TestCase):
    """证明线上预检没有消息发送表面，并且所有资源可异常安全清理。"""

    def test_success_only_opens_chat_list_and_closes_runtime(self):
        page = FakePage()
        context = FakeContext(page)
        browser = FakeBrowser(context)
        playwright = FakePlaywright()
        factory = lambda **_kwargs: (playwright, browser)
        users = [
            {
                "cookies": [
                    {"name": "fake", "value": "fake", "domain": ".example.test"}
                ]
            }
        ]

        result = run_preflight(
            users=users,
            runtime_config=RUNTIME_CONFIG,
            browser_factory=factory,
        )

        self.assertEqual(result, 1)
        self.assertEqual(page.visited_url, "https://www.douyin.com/chat")
        self.assertEqual(page.wait_until, "domcontentloaded")
        self.assertEqual(context.routes[0][0], "**/*")
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright.stopped)

    def test_failed_selector_still_closes_every_resource(self):
        page = FakePage(selector_error=RuntimeError("列表不存在"))
        context = FakeContext(page)
        browser = FakeBrowser(context)
        playwright = FakePlaywright()

        with self.assertRaises(PreflightError):
            run_preflight(
                users=[{"cookies": []}],
                runtime_config=RUNTIME_CONFIG,
                browser_factory=lambda **_kwargs: (playwright, browser),
            )

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright.stopped)


if __name__ == "__main__":
    unittest.main()
