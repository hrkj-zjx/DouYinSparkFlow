"""配置生成器的离线安全与健壮性测试。

测试只构造虚拟 Cookie，并通过 Node 调用前端导出的纯函数；整个过程不启动
浏览器、不访问网络，也不会读取或写入用户的真实 Cookie。
"""

import json
import shutil
import subprocess
import textwrap
import unittest
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values


# 从测试文件位置解析仓库根目录，避免依赖调用测试命令时的当前工作目录。
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MAIN_SCRIPT = REPOSITORY_ROOT / "docs" / "static" / "js" / "main.js"
INDEX_FILE = REPOSITORY_ROOT / "docs" / "index.html"
MAIN_STYLE = REPOSITORY_ROOT / "docs" / "static" / "css" / "main.css"
NODE_EXECUTABLE = shutil.which("node")


class ConfigGeneratorStaticTest(unittest.TestCase):
    """检查无需真实浏览器即可确认的 HTML、CSS 与脚本安全约束。"""

    def test_details_use_plain_text_and_no_config_is_logged(self) -> None:
        """详情弹窗不得启用 HTML 注入模式，也不得把配置写到控制台。"""

        script_source = MAIN_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("dangerouslyUseHTMLString", script_source)
        self.assertNotIn("console.", script_source)

    def test_document_has_semantics_and_required_time_guard(self) -> None:
        """页面应包含基础语义、视口设置、必填标记和不可清空的时间控件。"""

        html_source = INDEX_FILE.read_text(encoding="utf-8")
        self.assertTrue(html_source.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn('<html lang="zh-CN">', html_source)
        self.assertIn('name="viewport"', html_source)
        self.assertIn("<h1>", html_source)
        self.assertIn("<h2>", html_source)
        self.assertIn('required-hint', html_source)
        self.assertIn(':clearable="false"', html_source)
        self.assertNotIn('src="./static/js/icons-vue.js"', html_source)

    def test_css_has_valid_height_calculations(self) -> None:
        """回归原样式中缺少运算符空格、导致浏览器忽略高度规则的问题。"""

        css_source = MAIN_STYLE.read_text(encoding="utf-8")
        self.assertNotIn("100%-", css_source)
        self.assertIn("calc(100vh - 40px)", css_source)


@unittest.skipUnless(NODE_EXECUTABLE, "需要 Node.js 执行前端纯函数测试")
class ConfigGeneratorNodeTest(unittest.TestCase):
    """使用 Node 验证时间、XSS 文本和账户配置校验逻辑。"""

    def run_node_assertions(self, assertions: str) -> None:
        """在仓库根目录执行断言，并在失败时保留 Node 的完整诊断信息。"""

        node_program = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const generator = require("./docs/static/js/main.js");
            {assertions}
            """
        )
        completed_process = subprocess.run(
            [NODE_EXECUTABLE, "-e", node_program],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed_process.returncode != 0:
            # 仅测试失败时展示 Node 诊断；测试数据均为显式虚拟值，不包含真实 Cookie。
            self.fail(
                "Node 前端断言失败：\n"
                f"stdout:\n{completed_process.stdout}\n"
                f"stderr:\n{completed_process.stderr}"
            )

    def test_xss_payload_remains_literal_text(self) -> None:
        """含标签和事件属性的输入必须原样作为文本返回，不能被包装成 HTML。"""

        self.run_node_assertions(
            r"""
            const payload = '<img src=x onerror="globalThis.pwned=true"><script>x</script>';
            assert.equal(generator.formatDetailValue(payload), payload);
            const objectText = generator.formatDetailValue({ message: payload });
            assert.deepEqual(JSON.parse(objectText), { message: payload });
            assert.equal(globalThis.pwned, undefined);
            """
        )

    def test_null_and_invalid_times_fall_back_without_crashing(self) -> None:
        """null、空值和越界时间应稳定回退，合法时间则保持原值。"""

        self.run_node_assertions(
            r"""
            assert.equal(generator.normalizeRunTime(null), "09:00:00");
            assert.equal(generator.normalizeRunTime(""), "09:00:00");
            assert.equal(generator.normalizeRunTime("24:00:00"), "09:00:00");
            assert.equal(generator.normalizeRunTime("08:07:06"), "08:07:06");
            assert.deepEqual(generator.getCronParts(null), {
              hour: "09", minute: "00", second: "00"
            });
            const preview = generator.buildEnvironmentVariables({
              RUN_TIME: null,
              PROXY_ADDRESS: "",
              TZ: "Asia/Shanghai",
              MESSAGE_TEMPLATE: "test",
              HITOKOTO_TYPES: [],
              BROWSER_TIMEOUT: 120000,
              FRIEND_LIST_WAIT_TIME: 2000,
              TASK_RETRY_TIMES: 3,
              LOG_LEVEL: "Info",
              ACCOUNTS: [],
            });
            assert.equal(preview.CRON_HOUR, "09");
            assert.equal(preview.CRON_MINUTE, "00");
            assert.equal(preview.CRON_SECOND, "00");
            """
        )

    def test_valid_account_passes_central_validation(self) -> None:
        """完整的虚拟账户应通过复制操作共用的集中校验入口。"""

        self.run_node_assertions(
            r"""
            const form = {
              ACCOUNTS: [{
                username: "离线测试账户",
                unique_id: "demo_01",
                cookies: JSON.stringify([{
                  name: "fake_session",
                  value: "FAKE_VALUE_ONLY_FOR_TEST",
                  domain: ".example.invalid"
                }]),
                targets: ["虚拟好友"]
              }]
            };
            assert.equal(generator.validateConfiguration(form), true);
            assert.equal(
              generator.prepareValidatedCopyText(form, () => "safe-output"),
              "safe-output"
            );

            // 多行 Cookie JSON 在进入 .env 前应压缩为单行，同时保持数据语义不变。
            const prettyCookies = JSON.stringify(JSON.parse(
              form.ACCOUNTS[0].cookies
            ), null, 2);
            const normalizedCookies = generator.normalizeCookieJsonForEnvironment(
              prettyCookies
            );
            assert.equal(normalizedCookies.includes("\n"), false);
            assert.deepEqual(JSON.parse(normalizedCookies), JSON.parse(prettyCookies));
            """
        )

    def test_generated_dotenv_round_trips_special_characters(self) -> None:
        """生成的 .env 经生产同款解析器读取后必须保持特殊字符逐字一致。"""

        expected_message = "今日 #火花 ${HOME} O'Reilly \\ 路径\n下一行"
        node_program = textwrap.dedent(
            r"""
            const generator = require("./docs/static/js/main.js");
            process.stdout.write(generator.buildEnvFile({
              MESSAGE_TEMPLATE: "今日 #火花 ${HOME} O'Reilly \\ 路径\n下一行"
            }, {
              COOKIES_DEMO_01: JSON.stringify([{
                name: "fake_session",
                value: "${TOKEN} # ' \\",
                domain: ".example.invalid"
              }])
            }));
            """
        )
        completed_process = subprocess.run(
            [NODE_EXECUTABLE, "-e", node_program],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed_process.returncode, 0, completed_process.stderr)

        # 生产加载端显式关闭变量插值；这里使用相同选项验证井号、美元表达式、
        # 引号、反斜杠与字面换行均不会被截断或改写。
        parsed = dotenv_values(
            stream=StringIO(completed_process.stdout),
            interpolate=False,
        )
        self.assertEqual(parsed["MESSAGE_TEMPLATE"], expected_message.replace("\n", "\\n"))
        cookie = json.loads(parsed["COOKIES_DEMO_01"])
        self.assertEqual(cookie[0]["value"], "${TOKEN} # ' \\")

    def test_github_bundle_is_single_lossless_json_object(self) -> None:
        """GitHub 单一 Secret 应同时保留普通配置与 Cookie 的字面内容。"""

        self.run_node_assertions(
            r"""
            const variables = { MESSAGE_TEMPLATE: "今日 #火花 ${HOME}" };
            const secrets = { COOKIES_DEMO_01: '[{"value":"fake"}]' };
            const bundle = generator.buildGithubConfigJson(variables, secrets);
            assert.deepEqual(JSON.parse(bundle), { ...variables, ...secrets });
            """
        )

    def test_invalid_accounts_are_rejected_without_cookie_leakage(self) -> None:
        """验证必填、格式、唯一性及 Cookie 结构，且错误中不得出现敏感哨兵值。"""

        self.run_node_assertions(
            r"""
            const sentinel = "SENSITIVE_COOKIE_CANARY";
            const makeAccount = () => ({
              username: "测试账户",
              unique_id: "demo_01",
              cookies: JSON.stringify([{
                name: "fake_session",
                value: sentinel,
                domain: ".example.invalid"
              }]),
              targets: ["虚拟好友"]
            });
            const expectRejected = (accounts) => {
              assert.throws(
                () => generator.validateAccounts(accounts),
                (error) => {
                  assert.equal(error.name, "ConfigValidationError");
                  assert.equal(error.message.includes(sentinel), false);
                  return true;
                }
              );
            };

            const missingUsername = makeAccount();
            missingUsername.username = "  ";
            expectRejected([missingUsername]);

            const invalidUniqueId = makeAccount();
            invalidUniqueId.unique_id = "demo-01";
            expectRejected([invalidUniqueId]);

            const duplicateA = makeAccount();
            const duplicateB = makeAccount();
            duplicateB.unique_id = "DEMO_01";
            expectRejected([duplicateA, duplicateB]);

            const invalidJson = makeAccount();
            invalidJson.cookies = `not-json-${sentinel}`;
            expectRejected([invalidJson]);

            const emptyCookies = makeAccount();
            emptyCookies.cookies = "[]";
            expectRejected([emptyCookies]);

            const missingDomain = makeAccount();
            missingDomain.cookies = JSON.stringify([{
              name: "fake_session", value: sentinel
            }]);
            expectRejected([missingDomain]);

            // 与 Python 运行端一致，带完整 url 的 Playwright Cookie 也应合法。
            const urlCookie = makeAccount();
            urlCookie.cookies = JSON.stringify([{
              name: "fake_session",
              value: sentinel,
              url: "https://example.invalid/"
            }]);
            assert.equal(generator.validateAccounts([urlCookie]), true);

            const emptyTargets = makeAccount();
            emptyTargets.targets = [];
            expectRejected([emptyTargets]);

            // 校验失败时文本工厂绝不能执行，确保单项和整份复制都没有旁路。
            let factoryCalled = false;
            assert.throws(() => generator.prepareValidatedCopyText(
              { ACCOUNTS: [emptyTargets] },
              () => {
                factoryCalled = true;
                return sentinel;
              }
            ));
            assert.equal(factoryCalled, false);
            """
        )


if __name__ == "__main__":
    # 允许直接运行该文件进行离线回归，同时仍兼容仓库现有 unittest 发现机制。
    unittest.main()
