"use strict";

/**
 * 配置生成器的默认执行时间。时间选择器在正常交互中不允许清空，但这里仍保留
 * 防御性默认值，避免浏览器组件异常返回 null 时导致整个页面计算属性崩溃。
 */
const DEFAULT_RUN_TIME = "09:00:00";

/**
 * unique_id 会被拼进 COOKIES_<unique_id> 环境变量名，因此只允许环境变量名中
 * 稳定可用的字符。此约束与 Python 运行端保持一致，防止生成了无法读取的配置。
 */
const UNIQUE_ID_PATTERN = /^[A-Za-z0-9_]+$/;

/**
 * 专门表示用户配置错误。复制逻辑只向页面显示这类经过控制的消息，从而不会把
 * Cookie 原文或浏览器底层异常内容带进通知、控制台或日志。
 */
class ConfigValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = "ConfigValidationError";
  }
}

/**
 * 把时间选择器返回值收敛为 HH:mm:ss。传入 null、空串或越界时间时使用安全默认
 * 值，确保环境变量预览始终能够生成三个合法的 CRON 字段。
 */
function normalizeRunTime(value) {
  if (typeof value !== "string") {
    return DEFAULT_RUN_TIME;
  }

  const normalizedValue = value.trim();
  const timeMatch = normalizedValue.match(/^(\d{2}):(\d{2}):(\d{2})$/);
  if (!timeMatch) {
    return DEFAULT_RUN_TIME;
  }

  const [, hour, minute, second] = timeMatch;
  if (Number(hour) > 23 || Number(minute) > 59 || Number(second) > 59) {
    return DEFAULT_RUN_TIME;
  }
  return normalizedValue;
}

/**
 * 统一拆分定时字段，避免视图层直接对可能为 null 的 RUN_TIME 调用 split。
 */
function getCronParts(runTime) {
  const [hour, minute, second] = normalizeRunTime(runTime).split(":");
  return { hour, minute, second };
}

/**
 * 校验单个账户的 Cookie JSON。错误只包含账户序号、Cookie 项序号和字段名，绝不
 * 拼接用户输入内容；即使 JSON 中包含敏感令牌，错误提示也不会泄露该令牌。
 */
function validateCookieJson(rawCookies, accountPosition) {
  if (typeof rawCookies !== "string" || !rawCookies.trim()) {
    throw new ConfigValidationError(
      `第 ${accountPosition} 个账户的 Cookies 不能为空`
    );
  }

  let cookies;
  try {
    cookies = JSON.parse(rawCookies);
  } catch (_error) {
    throw new ConfigValidationError(
      `第 ${accountPosition} 个账户的 Cookies 不是有效 JSON`
    );
  }

  if (!Array.isArray(cookies) || cookies.length === 0) {
    throw new ConfigValidationError(
      `第 ${accountPosition} 个账户的 Cookies 必须是非空 JSON 数组`
    );
  }

  cookies.forEach((cookie, cookieIndex) => {
    const cookiePosition = cookieIndex + 1;
    if (!cookie || typeof cookie !== "object" || Array.isArray(cookie)) {
      throw new ConfigValidationError(
        `第 ${accountPosition} 个账户的 Cookie 第 ${cookiePosition} 项必须是对象`
      );
    }
    if (typeof cookie.name !== "string" || !cookie.name.trim()) {
      throw new ConfigValidationError(
        `第 ${accountPosition} 个账户的 Cookie 第 ${cookiePosition} 项缺少有效 name`
      );
    }
    // Cookie 的 value 合法情况下可以是空字符串，因此只校验字段类型，不把值写入错误消息。
    if (typeof cookie.value !== "string") {
      throw new ConfigValidationError(
        `第 ${accountPosition} 个账户的 Cookie 第 ${cookiePosition} 项缺少有效 value`
      );
    }
    const hasDomain =
      typeof cookie.domain === "string" && Boolean(cookie.domain.trim());
    const hasUrl = typeof cookie.url === "string" && Boolean(cookie.url.trim());
    if (!hasDomain && !hasUrl) {
      throw new ConfigValidationError(
        `第 ${accountPosition} 个账户的 Cookie 第 ${cookiePosition} 项缺少有效 domain 或 url`
      );
    }
  });

  return cookies;
}

/**
 * 集中校验所有账户字段。unique_id 按不区分大小写检查唯一性，因为生成 Secret
 * 名时会统一转为大写；这样可提前阻止两个账户覆盖同一个 COOKIES_* 环境变量。
 */
function validateAccounts(accounts) {
  if (!Array.isArray(accounts) || accounts.length === 0) {
    throw new ConfigValidationError("至少需要配置一个账户");
  }

  const seenUniqueIds = new Set();
  accounts.forEach((account, accountIndex) => {
    const accountPosition = accountIndex + 1;
    if (!account || typeof account !== "object" || Array.isArray(account)) {
      throw new ConfigValidationError(`第 ${accountPosition} 个账户配置无效`);
    }

    if (typeof account.username !== "string" || !account.username.trim()) {
      throw new ConfigValidationError(
        `第 ${accountPosition} 个账户的用户名不能为空`
      );
    }

    const uniqueId =
      typeof account.unique_id === "string" ? account.unique_id.trim() : "";
    if (!UNIQUE_ID_PATTERN.test(uniqueId)) {
      throw new ConfigValidationError(
        `第 ${accountPosition} 个账户的抖音号只能包含字母、数字和下划线`
      );
    }

    const normalizedUniqueId = uniqueId.toUpperCase();
    if (seenUniqueIds.has(normalizedUniqueId)) {
      throw new ConfigValidationError("不同账户的抖音号不能重复");
    }
    seenUniqueIds.add(normalizedUniqueId);

    validateCookieJson(account.cookies, accountPosition);

    if (!Array.isArray(account.targets) || account.targets.length === 0) {
      throw new ConfigValidationError(
        `第 ${accountPosition} 个账户至少需要填写一个目标好友`
      );
    }
    const hasInvalidTarget = account.targets.some(
      (target) => typeof target !== "string" || !target.trim()
    );
    if (hasInvalidTarget) {
      throw new ConfigValidationError(
        `第 ${accountPosition} 个账户的目标好友不能包含空值`
      );
    }
  });

  return true;
}

/**
 * 所有“复制单项”和“复制 .env”入口共享此校验函数，防止新增复制按钮时遗漏账户
 * 校验。实时预览不调用该函数，以免用户尚未填写完毕时打断表单操作。
 */
function validateConfiguration(form) {
  if (!form || typeof form !== "object") {
    throw new ConfigValidationError("配置表单不可用，请刷新页面后重试");
  }
  return validateAccounts(form.ACCOUNTS);
}

/**
 * 复制内容只能通过此函数生成：先完成整份表单校验，再调用文本工厂。把顺序写成
 * 可测试的纯函数，可证明校验失败时连敏感文本的生成步骤都不会执行。
 */
function prepareValidatedCopyText(form, textFactory) {
  validateConfiguration(form);
  if (typeof textFactory !== "function") {
    throw new ConfigValidationError("复制内容生成器不可用，请刷新页面后重试");
  }
  return textFactory();
}

/**
 * 把配置值转换成 .env 所需的单行文本。对象使用 JSON，普通字符串仅转义换行，
 * 不做 HTML 拼接或日志输出，因此用户内容不会进入可执行 HTML 上下文。
 */
function serializeConfigValue(value) {
  if (value !== null && typeof value === "object") {
    return JSON.stringify(value);
  }
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).replace(/\n/g, "\\n");
}

/**
 * 把单个值编码成 python-dotenv 可无损读取的单引号形式。反斜杠必须先转义，
 * 再处理单引号；生成端与 Python 加载端都禁用变量插值，因此 `${NAME}`、井号、
 * 空格和 Cookie JSON 会保持字面内容，不受服务器环境变量影响。
 */
function quoteDotenvValue(value) {
  const serializedValue = serializeConfigValue(value);
  return `'${serializedValue.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
}

/**
 * 详情弹窗只接收普通字符串。对象采用缩进 JSON 方便人工核对，返回值交由
 * Element Plus 的默认文本渲染处理，任何标签字符都会被当作文本而不是 HTML。
 */
function formatDetailValue(value) {
  if (value !== null && typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return value === null || value === undefined ? "" : String(value);
}

/**
 * Cookie-Editor 可能导出带缩进的多行 JSON。有效 JSON 在写入环境变量前压缩为
 * 单行，避免把结构换行错误地转换成 JSON 语法之外的 \\n；尚未填完或无效的内容
 * 原样留在实时预览中，最终仍会被复制前校验拦截。
 */
function normalizeCookieJsonForEnvironment(rawCookies) {
  if (typeof rawCookies !== "string") {
    return rawCookies;
  }
  try {
    return JSON.stringify(JSON.parse(rawCookies));
  } catch (_error) {
    return rawCookies;
  }
}

/**
 * 根据表单生成非敏感环境变量预览。该函数使用防御性时间拆分，但不执行必填
 * 校验；必填校验统一由复制门禁负责。
 */
function buildEnvironmentVariables(form) {
  const { hour, minute, second } = getCronParts(form.RUN_TIME);
  return {
    PROXY_ADDRESS: form.PROXY_ADDRESS,
    CRON_HOUR: hour,
    CRON_MINUTE: minute,
    CRON_SECOND: second,
    TZ: form.TZ,
    MESSAGE_TEMPLATE: form.MESSAGE_TEMPLATE,
    HITOKOTO_TYPES: form.HITOKOTO_TYPES,
    BROWSER_TIMEOUT: form.BROWSER_TIMEOUT,
    FRIEND_LIST_WAIT_TIME: form.FRIEND_LIST_WAIT_TIME,
    TASK_RETRY_TIMES: form.TASK_RETRY_TIMES,
    LOG_LEVEL: form.LOG_LEVEL,
    TASKS: form.ACCOUNTS.map((account) => ({
      username: account.username,
      unique_id: account.unique_id,
      targets: account.targets,
    })),
  };
}

/**
 * 根据账户生成敏感环境变量预览。键名统一转成大写，与 Python 运行端读取规则
 * 对齐；复制前的唯一性校验会阻止这里发生键覆盖。
 */
function buildEnvironmentSecrets(form) {
  return form.ACCOUNTS.reduce((secrets, account) => {
    const uniqueId = String(account.unique_id || "").trim().toUpperCase();
    // 空白账户尚未形成合法 Secret 名，预览中不显示容易误解的 COOKIES_ 空键。
    if (!uniqueId) {
      return secrets;
    }
    secrets[`COOKIES_${uniqueId}`] = normalizeCookieJsonForEnvironment(
      account.cookies
    );
    return secrets;
  }, {});
}

/**
 * 生成完整 .env 文本。每个值都走同一序列化规则，保证单项复制与整份复制结果
 * 一致，避免两条配置路径产生难以排查的差异。
 */
function buildEnvFile(environmentVariables, environmentSecrets) {
  return Object.entries({ ...environmentVariables, ...environmentSecrets })
    .map(([key, value]) => `${key}=${quoteDotenvValue(value)}`)
    .join("\n");
}

/**
 * GitHub Actions 只接收一个专用 Secret，避免工作流把仓库里的全部 Secrets 暴露
 * 给依赖安装后的 Python 进程。JSON 字符串由浏览器原生序列化，可直接保存为
 * ``DOUYIN_CONFIG_JSON``，其中的 Cookie 与普通配置保持原有类型和字面值。
 */
function buildGithubConfigJson(environmentVariables, environmentSecrets) {
  return JSON.stringify({ ...environmentVariables, ...environmentSecrets });
}

/**
 * 挂载浏览器界面。纯函数放在挂载逻辑之外，既方便离线 Node 测试，也保证测试
 * 过程不需要浏览器、网络或任何真实 Cookie。
 */
function mountConfigGenerator() {
  const { createApp, reactive, computed } = Vue;
  const app = createApp({
    setup() {
      /** 日志级别保持现有 Element Plus 下拉框的数据结构和视觉表现。 */
      const log_level_options = [
        { id: "Debug", label: "Debug", value: "Debug" },
        { id: "Info", label: "Info", value: "Info" },
        { id: "Warning", label: "Warning", value: "Warning" },
        { id: "Error", label: "Error", value: "Error" },
      ];

      /**
       * 首个账户保持为空，促使使用者主动填写必填信息，避免把示例 Cookie 或
       * 示例好友误当成真实配置复制到服务器。
       */
      const form = reactive({
        PROXY_ADDRESS: "",
        RUN_TIME: DEFAULT_RUN_TIME,
        TZ: "Asia/Shanghai",
        MESSAGE_TEMPLATE:
          "[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]",
        HITOKOTO_TYPES: ["文学", "影视", "诗词", "哲学"],
        BROWSER_TIMEOUT: 120000,
        FRIEND_LIST_WAIT_TIME: 2000,
        TASK_RETRY_TIMES: 3,
        LOG_LEVEL: "Info",
        ACCOUNTS: [
          {
            username: "",
            unique_id: "",
            cookies: "",
            targets: [],
          },
        ],
      });

      /** 实时预览采用纯函数生成，敏感值仅留在当前页面内存中。 */
      const environmentVariables = computed(() =>
        buildEnvironmentVariables(form)
      );
      const environmentSecrets = computed(() => buildEnvironmentSecrets(form));

      /**
       * 时间组件即使因浏览器边缘行为产生 null，也立即恢复默认值；配合页面上的
       * clearable=false，避免用户留下不完整的 CRON 配置。
       */
      const ensureRunTime = (value) => {
        form.RUN_TIME = normalizeRunTime(value);
      };

      /**
       * 集中执行“校验 -> 生成文本 -> 写剪贴板”。所有异常提示均为受控文案，不
       * 输出底层错误对象，避免浏览器实现把待复制内容意外附加到错误信息。
       */
      const writeValidatedText = (textFactory, successMessage) => {
        let text;
        try {
          text = prepareValidatedCopyText(form, textFactory);
        } catch (error) {
          const message =
            error instanceof ConfigValidationError
              ? error.message
              : "配置校验失败，请检查必填项后重试";
          ElementPlus.ElMessage.error(message);
          return Promise.resolve(false);
        }

        // 剪贴板 API 可能同步抛错或异步拒绝，两种路径均使用不含配置值的固定提示。
        try {
          return Promise.resolve(navigator.clipboard.writeText(text))
            .then(() => {
              ElementPlus.ElMessage.success(successMessage);
              return true;
            })
            .catch(() => {
              ElementPlus.ElMessage.error(
                "复制失败，请检查浏览器剪贴板权限后重试"
              );
              return false;
            });
        } catch (_error) {
          ElementPlus.ElMessage.error(
            "复制失败，请检查浏览器剪贴板权限后重试"
          );
          return Promise.resolve(false);
        }
      };

      /** 单项复制同样经过完整账户校验，不允许绕过 .env 复制门禁。 */
      const copyValue = (value) =>
        writeValidatedText(
          () => serializeConfigValue(value),
          "已复制到剪贴板"
        );

      /** 整份复制在校验通过后才读取当前计算属性并生成 .env。 */
      const copyEnvFile = () =>
        writeValidatedText(
          () =>
            buildEnvFile(
              environmentVariables.value,
              environmentSecrets.value
            ),
          "已复制 .env 配置文件到剪贴板"
        );

      /** GitHub 部署复制单一专用 Secret，同样经过账户与 Cookie 完整校验。 */
      const copyGithubConfig = () =>
        writeValidatedText(
          () =>
            buildGithubConfigJson(
              environmentVariables.value,
              environmentSecrets.value
            ),
          "已复制 DOUYIN_CONFIG_JSON"
        );

      /**
       * 详情内容直接作为纯文本交给 Element Plus；不能启用 HTML 字符串模式，也
       * 不在控制台输出 Cookie 或其他配置内容。
       */
      const openEnvDetails = (name, value) =>
        ElementPlus.ElMessageBox.alert(
          formatDetailValue(value),
          `${name} 详情`,
          {
            customClass: "env-detail-message",
            confirmButtonText: "关闭",
          }
        );

      /** 新增账户使用空白必填字段，避免示例值被误复制到生产配置。 */
      const addAccount = () => {
        form.ACCOUNTS.push({
          username: "",
          unique_id: "",
          cookies: "",
          targets: [],
        });
      };

      /** 页面至少保留一个账户；删除按钮在仅剩一个账户时由模板隐藏。 */
      const removeAccount = (index) => {
        if (form.ACCOUNTS.length > 1) {
          form.ACCOUNTS.splice(index, 1);
        }
      };

      return {
        log_level_options,
        form,
        environmentVariables,
        environmentSecrets,
        ensureRunTime,
        copyValue,
        copyEnvFile,
        copyGithubConfig,
        openEnvDetails,
        addAccount,
        removeAccount,
      };
    },
  });

  app.use(ElementPlus);
  app.mount("#app");
}

/**
 * CommonJS 导出仅供离线 Node 测试使用；浏览器直接加载脚本时 module 不存在，
 * 因而不会改变静态页面的执行方式或引入打包依赖。
 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DEFAULT_RUN_TIME,
    ConfigValidationError,
    normalizeRunTime,
    getCronParts,
    validateCookieJson,
    validateAccounts,
    validateConfiguration,
    prepareValidatedCopyText,
    serializeConfigValue,
    quoteDotenvValue,
    formatDetailValue,
    normalizeCookieJsonForEnvironment,
    buildEnvironmentVariables,
    buildEnvironmentSecrets,
    buildEnvFile,
    buildGithubConfigJson,
  };
}

/** 仅在真实网页环境中挂载，Node 离线测试不会访问 DOM 或浏览器剪贴板。 */
if (
  typeof window !== "undefined" &&
  typeof document !== "undefined" &&
  typeof Vue !== "undefined" &&
  typeof ElementPlus !== "undefined"
) {
  mountConfigGenerator();
}
