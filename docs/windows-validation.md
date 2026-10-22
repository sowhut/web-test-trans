# Windows 原生录制验收

状态：已实现后端并验证类路径算法；尚未完成 Windows 真机录制验收。

环境：Windows 10/11 交互式桌面，Python 3.8+，目标程序已经启动且提供 UI Automation Provider。工具与目标进程需要能够互相访问 UIA；不要在 UAC 安全桌面录制。多版本并存时使用 `py -3.8 -m venv .venv` 明确选择 Python 3.8。

1. 安装 `python -m pip install -e ".[windows,test]"`，运行 `python -m web_test_trans`。
2. 开始前先运行 `python -m uiautomation`（或使用 Windows Accessibility Insights）检查目标控件树。要求至少三个真实、可唯一识别的 ClassName 层级。
3. 打开本地录制台，选择 Windows 原生并填写目标 PID。确认会话中有独立的 `toast_listener_pid`。
4. 在至少三层 Frame/Panel 包含的输入框中输入普通文本，使用 Tab、Enter、Ctrl+A 等键盘操作；点击、双击、滚动、跨控件拖拽。
5. 密码控件必须暴露 `IsPassword=True`；会话中应仅有 `fill_secret` 和变量名，不出现密码或原始密码按键。自绘且未暴露密码语义的控件不属于可可靠脱敏范围。
6. 点击会出现通知的按钮。默认类名片段为 `toast,notification,snackbar`，需要时在界面补充目标通知类名。确认 toast 含三层类路径和 `after_action`。
7. 故意操作两个拥有完全相同类路径的控件，应显示无法唯一定位；不得出现 `index`、`foundIndex`、`[1]` 或 `[2]` 兜底。
8. 停止录制，确认钩子及 toast 进程均已退出。导出 ZIP，在新终端安装依赖、填写 `config.yml`/环境变量。
9. 重新打开目标程序，更新 PID，运行 `python test_recording.py`。检查键鼠结果与 toast 断言；模拟错误文案或没有 toast 的情况，断言必须失败。

原生键盘通过系统钩子采集，在入队前快照焦点与密码语义；鼠标目标由 UIA 工作线程解析。无响应应用可能使 UIA 查询变慢。极快的焦点切换、后台/隐藏控件可能导致定位失败；遇到警告应重录并检查代码。输入法组合、自绘控件和触摸/笔输入需要专门扩展，不在本次基本版验收范围。
