import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from .toast_listener import ToastListenerProcess

CAPTURE = Path(__file__).with_name("capture.js").read_text("utf-8")
TOAST_OBSERVER = Path(__file__).with_name("toast_observer.js").read_text("utf-8")


class WebRecorder:
    def __init__(self, store, session):
        self.store, self.session = store, session
        self.pw = self.browser = self.context = None
        self.pages = {}
        self.frame_paths = {}
        self.lock = asyncio.Lock()
        self.accepting = True
        self.pending = set()
        self.last_actions = {}
        self.toast_listener = ToastListenerProcess(store,session)

    async def start(self, credentials=None, headless=False):
        await self.toast_listener.start()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=headless)
        await self._context(credentials)

    async def _context(self, credentials):
        import json
        kwargs = {"viewport": {"width": 1360, "height": 900}}
        if credentials:
            origin = urlsplit(self.session["url"])
            kwargs["http_credentials"] = {**credentials, "origin": self.session.get('http_auth_origin') or f"{origin.scheme}://{origin.netloc}"}
            self.session["http_auth"] = True
        self.context = await self.browser.new_context(**kwargs)
        await self.context.expose_binding("__wtt_emit", self._receive)
        await self.context.expose_binding("__wtt_toast_emit", self._receive)
        toast_css = self.session.get("toast_selector")
        await self.context.add_init_script(CAPTURE + '\nwindow.__wtt.toastSelector = ' + json.dumps(toast_css or '') + ';\n' + TOAST_OBSERVER)
        self.context.on("page", self._page)
        page = await self.context.new_page()
        self.session["status"] = "recording"
        self.store.save(self.session)
        try:
            await page.goto(self.session["url"], wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            self.store.warn(self.session, f"页面导航未完成：{type(exc).__name__}；如遇浏览器认证，请填写 HTTP 认证信息后重试")

    def _task(self, coro):
        task = asyncio.create_task(coro)
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return task

    def _page(self, page):
        page_id = f"page_{len(self.pages)}"
        self.pages[page] = page_id
        page.on("framenavigated", lambda frame: self._task(self._navigated(page, frame)))
        page.on("response", lambda response: self._task(self._response(page, response)))
        page.on("close", lambda: self.store.append(self.session, {"type":"page_close", "page":page_id}) if self.accepting else None)
        self._task(self._opened(page))

    async def _opened(self, page):
        opener = await page.opener()
        self.store.append(self.session, {"type":"page_open", "page":self.pages[page], "opener":self.pages.get(opener)})

    async def _frames(self, frame):
        if frame in self.frame_paths:
            return self.frame_paths[frame]
        chain, current = [], frame
        while current.parent_frame:
            handle = await current.frame_element()
            chain.insert(0, await handle.evaluate("el => window.__wtt.selector(el)"))
            current = current.parent_frame
        self.frame_paths[frame] = chain
        return chain

    async def _navigated(self, page, frame):
        if not self.accepting:
            return
        if frame == page.main_frame and frame.url not in ('about:blank', ''):
            self.store.append(self.session, {"type":"navigation", "page":self.pages[page], "url":frame.url})
        if frame.parent_frame:
            try:
                self.frame_paths.pop(frame, None)
                await self._frames(frame)
            except Exception:
                pass  # The first interaction retries after the frame has initialized.

    async def _response(self, page, response):
        if not self.accepting or response.status not in (401, 407):
            return
        header = await response.header_value('www-authenticate' if response.status == 401 else 'proxy-authenticate')
        if header:
            scheme = header.split(' ', 1)[0]
            origin = urlsplit(response.url)
            auth_origin = f'{origin.scheme}://{origin.netloc}'
            if response.status == 401:
                self.session['http_auth_origin'] = auth_origin
            self.store.append(self.session, {"type":"auth_challenge", "page":self.pages[page],
                                           "status":response.status, "scheme":scheme, "origin":auth_origin})
            message = ("检测到 HTTP 认证弹窗，请在录制台填写账号密码后点击应用认证。" if response.status == 401 else
                       "检测到代理认证（407）；基础版需在启动浏览器前配置代理，不支持代理弹窗录制。")
            self.store.warn(self.session, message)

    async def _receive(self, source, payload):
        if not self.accepting or self.session["status"] != "recording":
            return
        async with self.lock:
            try:
                page_id = self.pages[source["page"]]
                if payload["type"] == "warning":
                    self.store.warn(self.session, payload["message"])
                    return
                frames = await self._frames(source["frame"])
                target = payload.pop("target")
                kind = 'toast' if payload["type"] == 'toast' else target["kind"]
                ref = self.store.register(kind, {"backend":"web", "css":target["css"], "frames":frames})
                event = {**payload, "component":ref, "page":page_id}
                if event.pop("secret", False):
                    event["secret_env"] = 'WTT_SECRET_' + ref.upper()
                    event.pop("value", None)
                if event.get("destination"):
                    dest = event["destination"]
                    event["destination"] = self.store.register(dest["kind"], {"backend":"web", "css":dest["css"], "frames":frames})
                if event['type'] == 'toast':
                    self.toast_listener.toast(event)
                    return
                if event["type"] == "dblclick":
                    for old in reversed(self.session['events']):
                        if old['type'] == 'click' and old.get('component') == ref and event['at'] - old['at'] < 700:
                            old['superseded'] = True
                            break
                saved = self.store.append(self.session, event)
                if event['type'] in ('click','dblclick','press','check','select'):
                    self.last_actions[page_id] = saved
                if event['type'] in ('click','dblclick','press','check','select','drag','fill','wheel'):
                    self.toast_listener.action(saved)
            except Exception as exc:
                self.store.warn(self.session, f"事件定位失败，未使用文本或坐标兜底：{exc}")

    async def authenticate(self, username, password):
        await self.context.close()
        await self._context({"username":username, "password":password})

    async def stop(self):
        # Flush browser bindings before closing the context.
        if self.context:
            for page in self.context.pages:
                for frame in page.frames:
                    try:
                        await frame.evaluate("() => { if(window.__wtt) window.__wtt.paused = true; }")
                    except Exception:
                        pass
        async with self.lock:
            self.accepting = False
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
        await self.toast_listener.stop()
        self.session['status'] = 'stopped'
        self.store.save(self.session)
