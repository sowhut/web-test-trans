"""Windows UIA backend. All locators use real ClassName paths, never Name/text."""
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def class_children(control, limit=2000):
    """Skip classless UIA wrappers; each returned control adds a real class filter."""
    result, queue = [], list(control.GetChildren())
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        if visited > limit:
            raise ValueError('UIA 控件树过大，无法安全生成定位')
        if current.ClassName:
            result.append(current)
        else:
            queue.extend(current.GetChildren())
    return result


def class_path(control, process_id):
    if not control.ClassName:
        raise ValueError('目标控件没有 ClassName，禁止文本/坐标定位兜底')
    chain, node = [], control
    while node and node.GetParentControl():
        if node.ClassName:
            chain.insert(0, node)
        node = node.GetParentControl()
    if len(chain) < 3:
        raise ValueError('原生控件不足三层真实 ClassName，无法满足定位约束')
    path = [{'class_name':current.ClassName} for current in chain]
    matched = resolve_class_path(node, path, process_id)
    if tuple(matched.GetRuntimeId()) != tuple(control.GetRuntimeId()):
        raise ValueError('UIA 类路径已失效')
    return path


def resolve_class_path(root, path, process_id):
    if len(path) < 3 or any(not step['class_name'] for step in path):
        raise ValueError('Windows 定位必须包含至少三层 ClassName')
    candidates = [root]
    for i, step in enumerate(path):
        if 'index' in step or 'count' in step:
            raise ValueError('禁止顺序索引定位')
        matches = [c for node in candidates for c in class_children(node) if c.ClassName == step['class_name']]
        if i == 0:
            matches = [c for c in matches if c.ProcessId == process_id]
        if not matches:
            raise AssertionError(f'控件类层级已变化：{step["class_name"]}，请重新录制')
        candidates = matches
    if len(candidates) != 1:
        raise AssertionError('类路径匹配多个控件；禁止按顺序选择，请为目标提供可区分的控件类层级')
    return next(iter(candidates))


def toast_controls(root, process_id, class_hints=('toast','notification','snackbar')):
    queue = [c for c in root.GetChildren() if c.ProcessId == process_id]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        if visited > 2000:
            raise ValueError('目标进程控件数超过 toast 扫描上限 2000')
        if not node.IsOffscreen and (any(h.lower() in node.ClassName.lower() for h in class_hints)
                                    or node.ControlTypeName == 'ToolTipControl'):
            yield node
        else:
            queue.extend(node.GetChildren())


class WindowsComponents:
    def __init__(self, definitions, process_id=0):
        if sys.platform != 'win32':
            raise RuntimeError('Windows 原生控件录制/回放仅支持 Windows 交互式桌面')
        import uiautomation as auto
        from pynput import keyboard, mouse
        self.auto, self.keyboard_module, self.mouse_module = auto, keyboard, mouse
        self.keyboard, self.mouse = keyboard.Controller(), mouse.Controller()
        self.process_id = process_id
        self.parameters = {}
        self.definitions = json.loads(Path(definitions).read_text('utf-8')) if isinstance(definitions, (str, Path)) else definitions

    def get(self, key):
        deadline = time.monotonic() + 5
        while True:
            try:
                return resolve_class_path(self.auto.GetRootControl(), self.definitions[key]['locator']['path'], self.process_id)
            except AssertionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)

    def perform(self, key, action, payload):
        control = self.get(key)
        rect = control.BoundingRectangle
        center = ((rect.left + rect.right)//2, (rect.top + rect.bottom)//2)
        if action in ('click', 'dblclick'):
            self.mouse.position = center
            button = getattr(self.mouse_module.Button, payload.get('button','left'))
            self.mouse.click(button, 2 if action == 'dblclick' else 1)
        elif action in ('key_down','key_up'):
            control.SetFocus()
            value = payload['key']
            if value.startswith('Key.'):
                key_value = getattr(self.keyboard_module.Key,value[4:])
            elif value.startswith('VK:'):
                key_value = self.keyboard_module.KeyCode.from_vk(int(value[3:]))
            else:
                key_value = value
            (self.keyboard.press if action == 'key_down' else self.keyboard.release)(key_value)
        elif action == 'fill_secret':
            name=payload['secret_env']
            control.GetValuePattern().SetValue(self.parameters[name] if name in self.parameters else os.environ[name])
        elif action == 'wheel':
            self.mouse.position = center
            self.mouse.scroll(payload['dx'], payload['dy'])
        elif action == 'drag':
            target = self.get(payload['destination']).BoundingRectangle
            end = ((target.left + target.right)//2, (target.top + target.bottom)//2)
            self.mouse.position = center
            self.mouse.press(self.mouse_module.Button.left)
            try:
                for n in range(1, 21):
                    self.mouse.position = (center[0]+(end[0]-center[0])*n/20, center[1]+(end[1]-center[1])*n/20)
                    time.sleep(.015)
            finally:
                self.mouse.release(self.mouse_module.Button.left)


@dataclass(frozen=True)
class NativeToastExpectation:
    component: str
    text: Optional[str] = None


class WindowsToastAssertions:
    def __init__(self, components):
        self.components = components

    def _sample(self, item):
        try:
            c = resolve_class_path(self.components.auto.GetRootControl(),
                                   self.components.definitions[item.component]['locator']['path'],
                                   self.components.process_id)
            return c.Name if not c.IsOffscreen else None
        except (AssertionError, LookupError):
            return None

    def assert_visible(self, expected, timeout_ms=5000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            value = self._sample(expected)
            if value is not None and (expected.text is None or expected.text == value):
                return
            time.sleep(.025)
        raise AssertionError(f'未发现原生 toast：{expected}')

    def assert_after_click(self, click, expected, timeout_ms=5000):
        if isinstance(expected, NativeToastExpectation):
            expected = [expected]
        stop, ready = threading.Event(), threading.Event()
        found, errors = set(), []
        def observe():
            try:
                with self.components.auto.UIAutomationInitializerInThread():
                    baseline = [self._sample(e) for e in expected]
                    ready.set()
                    while not stop.is_set():
                        for i,e in enumerate(expected):
                            value = self._sample(e)
                            if value is not None and value != baseline[i] and (e.text is None or value == e.text):
                                found.add(i)
                            baseline[i] = value
                        stop.wait(.025)
            except Exception as exc:
                errors.append(exc)
                ready.set()
        thread = threading.Thread(target=observe, daemon=True)
        thread.start()
        try:
            if not ready.wait(5):
                raise TimeoutError('原生 toast 监听初始化超时')
            if errors:
                raise errors[0]
            result = click()
            deadline = time.monotonic() + timeout_ms / 1000
            while len(found) != len(expected) and time.monotonic() < deadline and not errors:
                time.sleep(.025)
            if errors:
                raise errors[0]
            if len(found) != len(expected):
                raise AssertionError(f'点击后未出现全部原生 toast：{expected}')
            return result
        finally:
            stop.set()
            thread.join(timeout=2)
