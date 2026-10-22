"""Dedicated OS process for toast observation and action correlation.

Web DOM observations arrive over a separate Playwright binding. Native UIA is
polled inside this child process, independently of the keyboard/mouse worker.
Only the parent writes sessions and the reusable component registry.
"""
import asyncio
import multiprocessing
import queue
import time
from contextlib import nullcontext

from .compat import run_in_thread


def _worker(inbox, outbox, ready, settings):
    actions, seen = {}, {}
    auto = None
    try:
        if settings['backend'] == 'windows':
            import uiautomation as auto
        initializer = auto.UIAutomationInitializerInThread() if auto else nullcontext()
        with initializer:
            ready.set()
            last_scan = 0
            while True:
                try:
                    message = inbox.get(timeout=.025)
                except queue.Empty:
                    message = None
                if message and message['type'] == 'shutdown':
                    break
                if message and message['type'] == 'action':
                    event = message['event']
                    actions[event.get('page','native')] = event
                if message and message['type'] == 'toast':
                    event = message['event']
                    _correlate(event, actions, settings['toast_window_ms'])
                    outbox.put(event)
                if auto and time.monotonic() - last_scan > .05:
                    last_scan = time.monotonic()
                    from .windows_runtime import class_path, toast_controls
                    current = {}
                    try:
                        for control in toast_controls(auto.GetRootControl(),settings['process_id'],settings['toast_classes']):
                            rid, value = tuple(control.GetRuntimeId()), control.Name
                            current[rid] = value
                            if seen.get(rid) != value:
                                event = {'type':'toast','text':value,'at':time.time()*1000,
                                         'locator':{'backend':'windows','path':class_path(control,settings['process_id'])}}
                                _correlate(event,actions,settings['toast_window_ms'])
                                outbox.put(event)
                        seen = current
                    except Exception as exc:
                        outbox.put({'type':'warning','message':f'原生 toast 监听：{type(exc).__name__}: {exc}'})
    except Exception as exc:
        outbox.put({'type':'warning','message':f'Toast 监听进程异常：{type(exc).__name__}: {exc}'})
        ready.set()


def _correlate(event, actions, window_ms):
    action = actions.get(event.get('page','native'))
    if action and 0 <= event['at'] - action['at'] <= window_ms:
        event['after_action'] = action['id']


class ToastListenerProcess:
    def __init__(self, store, session):
        self.store,self.session=store,session
        self.ctx=multiprocessing.get_context('spawn')
        self.inbox,self.outbox=self.ctx.Queue(),self.ctx.Queue()
        self.ready=self.ctx.Event()
        self.process=None
        self.draining=False
        self.stopping=False

    async def start(self):
        settings={k:self.session.get(k) for k in ('backend','process_id','toast_window_ms')}
        settings['toast_classes']=self.session.get('toast_classes') or ['toast','notification','snackbar']
        self.process=self.ctx.Process(target=_worker,args=(self.inbox,self.outbox,self.ready,settings),
                                     name='wtt-toast-listener',daemon=True)
        self.process.start()
        if not await run_in_thread(self.ready.wait,10):
            self.process.terminate()
            raise TimeoutError('Toast 监听进程启动超时')
        self.session['toast_listener_pid']=self.process.pid
        self.draining=True
        self.drain_task=asyncio.create_task(self._drain())

    def action(self,event):
        self.inbox.put({'type':'action','event':event})

    def toast(self,event):
        self.inbox.put({'type':'toast','event':event})

    def _collect(self):
        while True:
            try:
                event=self.outbox.get_nowait()
            except queue.Empty:
                return
            if event['type']=='warning':
                self.store.warn(self.session,event['message'])
            else:
                if 'locator' in event:
                    event['component']=self.store.register('toast',event.pop('locator'))
                self.store.append(self.session,event)

    async def _drain(self):
        while self.draining:
            self._collect()
            if self.process and not self.process.is_alive() and not self.stopping:
                self.store.warn(self.session,'Toast 监听进程已退出；请停止录制并重新启动')
                return
            await asyncio.sleep(.025)

    async def stop(self):
        if not self.process:
            return
        self.stopping=True
        self.inbox.put({'type':'shutdown'})
        await run_in_thread(self.process.join,5)
        if self.process.is_alive():
            self.process.terminate()
            await run_in_thread(self.process.join,2)
            self.store.warn(self.session,'Toast 监听进程未正常退出，末尾提示可能不完整')
        self.draining=False
        await self.drain_task
        self._collect()
        self.inbox.close();self.outbox.close()
