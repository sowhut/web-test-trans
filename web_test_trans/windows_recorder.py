"""Native Windows recorder: hooks → UIA worker → class registry → replay."""
import asyncio
import queue
import sys
import threading
import time

from .windows_runtime import class_path
from .toast_listener import ToastListenerProcess
from .compat import run_in_thread


def _control_kind(control):
    name = control.ControlTypeName
    return (name[:-len('Control')] if name.endswith('Control') else name).lower()


class WindowsRecorder:
    def __init__(self, store, session):
        self.store, self.session = store, session
        self.queue = queue.Queue(maxsize=10000)
        self.closed, self.ready = threading.Event(), threading.Event()
        self.error = None
        self.listeners = []
        self.last_action = None
        self.secret_ref = None
        self.toast_listener = ToastListenerProcess(store,session)

    async def start(self):
        if sys.platform != 'win32':
            raise RuntimeError('请在 Windows 上安装 .[windows] 并运行，macOS 无法录制原生 Windows Frame')
        import uiautomation as auto
        from pynput import keyboard, mouse
        await self.toast_listener.start()
        self.auto = auto
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()
        if not await run_in_thread(self.ready.wait, 10):
            self.closed.set()
            raise TimeoutError('Windows UI Automation 初始化超时')
        if self.error:
            raise RuntimeError(self.error)
        def enqueue(kind, **payload):
            try:
                self.queue.put_nowait({'type':kind,'at':time.time()*1000, **payload})
            except queue.Full:
                self.store.warn(self.session, '原生事件队列已满，部分事件未录制')
        cached_focus = {}
        def keyboard_event(kind, key):
            # Snapshot focus and password semantics before queueing. Resolving only
            # later in the worker can leak password keys after a fast focus change.
            try:
                with auto.UIAutomationInitializerInThread():
                    focused = auto.GetFocusedControl()
                    if not focused or focused.ProcessId != self.session['process_id']:
                        return
                    identity = tuple(focused.GetRuntimeId())
                    if cached_focus.get('identity') != identity:
                        cached_focus.update(identity=identity,
                            locator={'backend':'windows','path':class_path(focused,self.session['process_id'])},
                            control_kind=_control_kind(focused))
                    sensitive = bool(focused.IsPassword)
                    if sensitive:
                        safe_key = str(key) if key in (keyboard.Key.tab,keyboard.Key.enter,keyboard.Key.esc) else None
                    elif isinstance(key,keyboard.Key):
                        safe_key = str(key)
                    elif getattr(key,'vk',None) is not None:
                        safe_key = 'VK:' + str(key.vk)
                    else:
                        safe_key = key.char
                    enqueue(kind,locator=cached_focus['locator'],control_kind=cached_focus['control_kind'],
                            secret=sensitive,key=safe_key)
            except Exception as exc:
                self.store.warn(self.session,f'键盘目标无法安全识别，已跳过：{type(exc).__name__}')
        self.listeners = [
            mouse.Listener(on_click=lambda x,y,b,pressed: enqueue('mouse_down' if pressed else 'mouse_up', x=x,y=y,button=b.name),
                           on_scroll=lambda x,y,dx,dy: enqueue('wheel',x=x,y=y,dx=dx,dy=dy)),
            keyboard.Listener(on_press=lambda key: keyboard_event('key_down',key),
                              on_release=lambda key: keyboard_event('key_up',key))
        ]
        self.session['status'] = 'recording'
        self.store.save(self.session)
        for listener in self.listeners:
            listener.start()

    def _ref(self, control, kind=None):
        return self.store.register(kind or _control_kind(control),
                                   {'backend':'windows','path':class_path(control,self.session['process_id'])})

    def _worker(self):
        try:
            with self.auto.UIAutomationInitializerInThread():
                root = self.auto.GetRootControl()
                if not any(c.ProcessId == self.session['process_id'] for c in root.GetChildren()):
                    raise ValueError('未找到目标进程的桌面窗口，请确认 PID 并先启动程序')
                self.ready.set()
                down, last_click = None, None
                while not self.closed.is_set() or not self.queue.empty():
                    try:
                        event = self.queue.get(timeout=.05)
                    except queue.Empty:
                        event = None
                    if event:
                        try:
                            if 'locator' in event:
                                ref = self.store.register(event.pop('control_kind'),event.pop('locator'))
                                sensitive = event.pop('secret')
                            else:
                                control = self.auto.ControlFromPoint(event['x'],event['y'])
                                if not control or control.ProcessId != self.session['process_id']:
                                    self.secret_ref = None
                                    down = None
                                    continue
                                ref = self._ref(control)
                                sensitive = False
                            event['component'] = ref
                            event.pop('x', None)
                            event.pop('y', None)
                            if event['type'].startswith('key_') and sensitive:
                                if event['key'] is None:
                                    if self.secret_ref != ref and event['type']=='key_down':
                                        self.store.append(self.session, {'type':'fill_secret','component':ref,
                                            'secret_env':'WTT_SECRET_' + ref.upper(), 'at':event['at']})
                                        self.secret_ref = ref
                                    continue
                            else:
                                self.secret_ref = None
                            if event['type'] == 'mouse_down':
                                down = event
                            elif event['type'] == 'mouse_up':
                                if down and down['component'] != ref and down['button'] == 'left':
                                    event = {**down, 'type':'drag', 'destination':ref}
                                else:
                                    event['type'] = 'click'
                                    if last_click and ref == last_click['component'] and event['button'] == last_click['button'] and event['at'] - last_click['at'] < 500:
                                        last_click['superseded'] = True
                                        event['type'] = 'dblclick'
                                    last_click = event
                                down = None
                            saved = self.store.append(self.session, event)
                            if event['type'] in ('click','dblclick','key_down','key_up','drag','wheel'):
                                self.last_action = saved
                                self.toast_listener.action(saved)
                        except Exception as exc:
                            self.store.warn(self.session, f'原生事件未录制：{type(exc).__name__}: {exc}')
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
            self.session['status'] = 'error'
            self.store.warn(self.session, self.error)

    async def stop(self):
        for listener in self.listeners:
            listener.stop()
        self.closed.set()
        if hasattr(self,'thread'):
            await run_in_thread(self.thread.join, 5)
            if self.thread.is_alive():
                raise RuntimeError('UIA 工作线程尚未退出，请关闭目标程序后重试停止')
        await self.toast_listener.stop()
        self.session['status'] = 'stopped'
        self.store.save(self.session)
