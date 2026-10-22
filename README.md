# Web Test Trans

Python 本地自动化录制台。浏览器录制导出可独立运行的 Playwright Python 项目；Windows 原生 Frame/窗口控件导出 UI Automation Python 项目。中文 Web 界面管理录制、组件库、代码预览和 ZIP 导出。

## 快速开始

需要 Python 3.10+（推荐 3.12）。

```bash
git clone https://github.com/sowhut/web-test-trans.git
cd web-test-trans
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 使用：.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m playwright install chromium
python -m web_test_trans
```

打开 <http://127.0.0.1:8765>。填写 URL，或点击「使用内置演示页面」，再点击「开始录制」。在新开的 Chromium 窗口操作，回到录制台停止并导出。

Windows 原生模式额外安装：

```powershell
python -m pip install -e ".[windows]"
python -m web_test_trans
```

先打开目标桌面程序，从任务管理器「详细信息」取得 PID，选择「Windows 原生」并填写 PID。录制仅收集该进程的控件操作。原生窗口没有 CSS DOM，使用 UIA `ClassName` 路径，不能交给 Playwright 操作。

使用 uv 可复现安装：`uv sync --extra test`，Windows 加 `--extra windows`；`uv run playwright install chromium`；`uv run web-test-trans`。`uv.lock` 已锁定依赖。

## 严格的定位约束

- 网页每个交互目标与每层 iframe 均至少包含 **3 个真实 DOM 层级**，例如 `section.profile > div.actions > button.save`。
- 只生成 CSS：标签、class、稳定 ID、`data-testid` / `data-test` / `data-qa` / `name` / `type` / `role` 属性。属性值不取自可见文案。
- **禁止** `get_by_text`、`has_text`、`:has-text`、XPath，以及 `.nth()`、`nth-child`、`nth-of-type`、`[1]` / `[2]`、首尾元素选择等顺序定位。
- 重复 div 会尝试更完整的祖先层级与稳定属性组合。如果仍不唯一，事件被明确标记为未录制，界面显示原因。不会随意选择某个元素。建议给被测页面补充稳定 class 或 `data-testid`。
- Windows 跳过没有 ClassName 的中间包装节点，保留至少三层真实类名。用整个类路径及目标进程确认唯一性；同类同路径重复时拒绝录制，不按控件顺序猜选。
- Toast 属于通知集合：用三层 CSS 找到整个集合，对出现的内容断言。多个相同 class 的 toast 可以共存，无需按索引选取；文案仅作为期望值，不参与定位。

## 录制能力

| 能力 | 基础版行为 |
| --- | --- |
| 网页键盘 | 文本输入、中文输入法最终值、常用快捷键/导航键；保留原始按下/释放事件，导出语义操作避免重复输入 |
| 网页鼠标 | 左/右/中键点击、双击、停留悬停、横纵滚轮、元素间拖拽 |
| 表单 | 文本框、密码框、checkbox/radio、原生 select |
| 页面 | 导航、新窗口、同源/跨域/嵌套 iframe |
| 组件复用 | 按钮、输入框、选择框、勾选框、Frame 等类型封装；相同结构定位跨录制复用同一个组件 key |
| Toast | 自动识别点击及其他语义操作后的通知，记录定位、文案和触发事件，导出断言 |
| HTTP 认证 | 识别带 `WWW-Authenticate` 的 HTTP 401；Basic/Digest 通过 `http_credentials`；导出 `config.yml` 参数 |
| 原生 Windows | UIA ClassName 路径，键盘按下/释放、点击、双击、滚轮、元素间拖拽，目标进程内的 toast 扫描 |

组件库保存在 `data/components.json`，会话保存在 `data/session_*.json`。`data/`、`exports/`、`.env` 都已忽略，不会随代码提交。服务绑定 `127.0.0.1`，拒绝跨站 Origin 请求。只运行一个服务进程使用同一数据目录。

## 独立 Toast 监听进程与断言接口

每次录制创建一个名为 `wtt-toast-listener` 的独立 OS 进程，PID 保存在会话的 `toast_listener_pid` 中：

1. 网页的独立 DOM Observer 通过专用 `__wtt_toast_emit` 通道发送通知，键鼠通过另一通道发送动作；Playwright 页面句柄留在父进程。
2. 监听进程通过进程间队列接收通知与动作，按页面及时间窗口关联触发事件；父进程统一写文件。
3. Windows 模式由该进程自己扫描目标进程的 UIA 通知控件，和键鼠采集线程独立运行。
4. 停止录制会排空事件并回收子进程。Toast 默认观察窗口 5 秒，可以在界面调整。

默认识别 `role=alert/status`、Ant Design、Element、Toastify、Vuetify、Quasar 及 `.toast/.snackbar`。界面可以补充自定义 Toast CSS 或原生类名片段。普通页面上持久的 `role=status` 也可能被识别，需要检查导出的断言是否符合业务。

导出项目直接提供：

```python
from runtime import ComponentLibrary, ToastAssertions, ToastExpectation

components = ComponentLibrary("components.json")
toasts = ToastAssertions(components)

# key 可在录制台的「组件定位库」复制；只调用已封装的定位。
save = components.button("button_<hash>")
toasts.assert_after_click(
    page,
    lambda: save.click(page),
    [ToastExpectation("toast_<hash>", text="保存成功")],
    timeout_ms=5000,
)
toasts.assert_visible(page, ToastExpectation("toast_<hash>", text="保存成功"))
components.input("input_<hash>").fill(page, "新的输入")
```

`assert_after_click()` 在动作之前安装监听，记录期间出现过的 toast；即使断言运行时 toast 已消失也可验证。该接口也可包装拖拽、键盘等操作。`text=None` 表示只断言出现，不校验文案。

Windows 导出提供相同用途的 `WindowsToastAssertions`、`NativeToastExpectation`，使用类路径查找。原生 toast 通过轮询识别，寿命短于扫描周期的通知可能漏采；不是跨进程 Windows 系统通知中心的通用录制器。

## 浏览器原生账号密码弹窗 → YAML 参数

HTTP Basic/Digest 弹窗不在 DOM 内，无法使用 CSS 记录弹窗输入框。检测到 401 后，录制台显示提示。在「Toast 与认证设置」填入用户名和密码，点击「应用认证并重新打开页面」；也可以在开始录制前填好。

导出结构：

```text
test_recording.py       # 可读的组件调用与断言
components.json        # 共享组件定位
runtime.py             # 网页组件与 ToastAssertions
# 或 windows_runtime.py
configuration.py       # YAML 加载与环境变量解析
config.yml             # 回放参数，包括 HTTP 认证
recording.json         # 录制事件、警告、监听进程信息
requirements.txt
.env.example
README.txt
```

`config.yml` 示例：

```yaml
recording:
  backend: web
  url: https://example.com
parameters:
  WTT_SECRET_INPUT_EXAMPLE: ${WTT_SECRET_INPUT_EXAMPLE}
browser:
  headless: false
  viewport:
    width: 1360
    height: 900
  http_credentials:
    origin: https://example.com
    username: ${WTT_HTTP_USERNAME}
    password: ${WTT_HTTP_PASSWORD}
```

账号密码参数保留在 YAML 中，默认使用环境变量占位。可在本地 YAML 填实际值，或设置环境变量：

```powershell
$env:WTT_HTTP_USERNAME = "your-user"
$env:WTT_HTTP_PASSWORD = "your-password"
# .env.example 列出的网页密码变量也应设置。
python -m pip install -r requirements.txt
python -m playwright install chromium
python test_recording.py
```

macOS/Linux 使用 `export WTT_HTTP_USERNAME=...`。`WTT_HEADLESS=1` 可无头回放。`.env` 不自动加载。Windows 原生回放设置 `WTT_PROCESS_ID` 为当前运行目标的 PID，或修改 `config.yml` 中的 `windows.process_id`。

网页 password 输入与其原始按键被脱敏；HTTP 认证信息只传给浏览器上下文，不写到会话或导出中。普通业务输入、URL 和 toast 文案会保存，分享录制文件前自行检查。

## 验证与边界

```bash
python -m pip install -e ".[test]"
python -m playwright install chromium
python -m pytest -q
```

测试覆盖真实 Chromium 的录制→导出→回放、键鼠、iframe、多个同类 toast、短时 toast、密码脱敏、HTTP 认证、独立监听进程及原生类路径解析。GitHub Actions 运行 Linux 浏览器测试和 Windows 核心逻辑测试。

**Windows 原生实际录制尚需在 Windows 交互式桌面验收。** 当前开发环境是 macOS，不能将模拟 UIA 树测试当作 Windows 真机验收。Windows 验收步骤见 [docs/windows-validation.md](docs/windows-validation.md)。

基础版暂不支持：Shadow DOM、canvas 内控件、浏览器地址栏操作、系统文件选择器、系统权限/证书弹窗、密码管理器/自动填充面板、Windows UAC 安全桌面、无法暴露 UIA ClassName 的控件。同一控件内部的绘图/自由轨迹拖动不属于元素间拖拽。文件选择会提示手动补充 `set_input_files()`。

HTTP 401 处理不是录制浏览器弹窗的物理按键。当前一个浏览器上下文配置一个认证 origin；代理 407 会提示，但不自动设置代理凭据。点击引起整页导航时的瞬时 toast、点击后才新建 iframe 内的瞬时 toast，需要业务特定等待/断言，基础版不能保证捕获。录制时间关联也不能证明业务因果，快速连续点击应检查断言归属。

官方 API 参考：[Playwright 事件注入与绑定](https://playwright.dev/python/docs/api/class-browsercontext)、[Playwright 输入](https://playwright.dev/python/docs/input)、[Windows UI Automation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)。
