import asyncio
import platform
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .exporter import export_bundle, generate, normalized_events
from .store import Store
from .web_recorder import WebRecorder
from .windows_recorder import WindowsRecorder

STATIC = Path(__file__).with_name('static')


class StartRequest(BaseModel):
    name: str = Field(default='未命名录制', min_length=1, max_length=100)
    backend: Literal['web','windows'] = 'web'
    url: str = ''
    process_id: int = Field(default=0, ge=0)
    toast_window_ms: int = Field(default=5000, ge=500, le=30000)
    toast_selector: str = Field(default='', max_length=1000)
    toast_classes: list[str] = Field(default_factory=list, max_length=30)
    username: str = ''
    password: SecretStr = SecretStr('')


class AuthRequest(BaseModel):
    username: str
    password: SecretStr


def create_app(data_dir='data'):
    store = Store(data_dir)
    state = {'recorder':None}
    transition = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        if state['recorder']:
            await state['recorder'].stop()

    app = FastAPI(title='Web Test Trans', version='0.1.0', lifespan=lifespan)
    app.state.store = store
    app.state.recorder_state = state
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1','localhost','[::1]','testserver'])

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        origin = request.headers.get('origin')
        if request.url.path.startswith('/api/') and origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
            return JSONResponse({'detail':'仅允许本地录制台的同源请求'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def get_session(session_id):
        try:
            return store.get(session_id)
        except KeyError:
            raise HTTPException(404, '录制不存在')

    @app.get('/api/status')
    async def status():
        recorder = state['recorder']
        return {'platform':platform.system(),'windows_available':platform.system() == 'Windows',
                'active_id':recorder.session['id'] if recorder else None,'version':'0.1.0'}

    @app.get('/api/sessions')
    async def sessions():
        return store.list()

    @app.post('/api/sessions')
    async def start(request: StartRequest):
        async with transition:
            if state['recorder']:
                raise HTTPException(409, '请先停止当前录制')
            if request.backend == 'web':
                url = urlsplit(request.url)
                if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
                    raise HTTPException(422, '请输入 HTTP/HTTPS URL，认证信息请填入专用字段')
            elif platform.system() != 'Windows' or not request.process_id:
                raise HTTPException(422, '原生录制需要 Windows 系统和目标程序 PID')
            session = store.create(request.name,request.backend,url=request.url,process_id=request.process_id,
                                   toast_window_ms=request.toast_window_ms,toast_selector=request.toast_selector,
                                   toast_classes=request.toast_classes,http_auth=False)
            recorder = WebRecorder(store,session) if request.backend == 'web' else WindowsRecorder(store,session)
            state['recorder'] = recorder
            try:
                if request.backend == 'web':
                    credentials = {'username':request.username,'password':request.password.get_secret_value()} if request.username else None
                    await recorder.start(credentials=credentials)
                else:
                    await recorder.start()
            except Exception as exc:
                try:
                    await recorder.stop()
                finally:
                    state['recorder'] = None
                session['status'] = 'error'
                store.warn(session,f'启动失败：{type(exc).__name__}。请检查浏览器安装或 Windows 依赖。')
                store.save(session)
                raise HTTPException(500,'启动失败，请确认已执行 playwright install chromium；Windows 模式需安装 .[windows]')
            return session

    @app.get('/api/sessions/{session_id}')
    async def session(session_id: str):
        result = get_session(session_id)
        result['action_count'] = len(normalized_events(result))
        return result

    @app.post('/api/sessions/{session_id}/stop')
    async def stop(session_id: str):
        async with transition:
            recorder = state['recorder']
            if not recorder or recorder.session['id'] != session_id:
                raise HTTPException(409,'该录制未运行')
            await recorder.stop()
            state['recorder'] = None
            return recorder.session

    @app.post('/api/sessions/{session_id}/http-auth')
    async def authenticate(session_id: str, request: AuthRequest):
        async with transition:
            recorder = state['recorder']
            if not isinstance(recorder,WebRecorder) or recorder.session['id'] != session_id:
                raise HTTPException(409,'请选择正在录制的网页会话')
            await recorder.authenticate(request.username,request.password.get_secret_value())
            return {'ok':True}

    @app.get('/api/sessions/{session_id}/code', response_class=PlainTextResponse)
    async def code(session_id: str):
        return generate(get_session(session_id),store.components)

    @app.get('/api/sessions/{session_id}/export')
    async def export(session_id: str):
        result = get_session(session_id)
        if result['status'] in ('recording','starting','stopping'):
            raise HTTPException(409,'请先停止录制，确保导出包含完整事件')
        return Response(export_bundle(result,store.components),media_type='application/zip',
                        headers={'Content-Disposition':f'attachment; filename="recording-{session_id[:8]}.zip"'})

    @app.get('/api/components')
    async def components():
        return store.components

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/demo')
    async def demo():
        return FileResponse(STATIC / 'demo.html')

    @app.get('/demo/frame')
    async def demo_frame():
        return FileResponse(STATIC / 'frame.html')

    app.mount('/static',StaticFiles(directory=STATIC),name='static')
    return app
