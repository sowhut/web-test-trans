import json
import sys
import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from web_test_trans.app import create_app
from web_test_trans.exporter import export_bundle, generate, normalized_events, make_config
from web_test_trans.store import Store, validate_locator
from web_test_trans.windows_runtime import class_path, resolve_class_path


def test_registry_reuses_across_sessions_and_restart(tmp_path):
    store = Store(tmp_path)
    locator = {'backend':'web','css':'main > div.actions > button.save','frames':[]}
    a = store.register('button',locator)
    b = store.register('button',locator)
    assert a == b
    assert Store(tmp_path).components[a]['locator'] == locator
    session=store.create('test','web')
    store.append(session,{'type':'navigation','url':'https://example.com'})
    summaries=store.list()
    assert summaries[0]['id']==session['id'] and summaries[0]['event_count']==1
    assert 'events' not in summaries[0]
    session['status']='recording';store.save(session)
    assert Store(tmp_path).get(session['id'])['status'] == 'interrupted'


@pytest.mark.parametrize('selector',['button.save','div > button', 'main > div > :has-text("save")','main > div > text=Save', 'main > div > div:nth-of-type(2)', 'main > div > div:nth-child(1)', 'main > div > div[1]'])
def test_reject_noncompliant_selectors(selector):
    with pytest.raises(ValueError):
        validate_locator({'backend':'web','css':selector})


class Control:
    n = 0
    def __init__(self, cls, parent=None, pid=7):
        Control.n += 1
        self.ClassName,self.parent,self.ProcessId,self.id = cls,parent,pid,Control.n
        self.children=[]
        if parent: parent.children.append(self)
    def GetChildren(self): return self.children
    def GetParentControl(self): return self.parent
    def GetRuntimeId(self): return [self.id]


def test_native_three_classes_skips_classless_wrappers_and_detects_drift():
    root=Control('Desktop');window=Control('AppWindow',root);frame=Control('Frame',window)
    wrapper=Control('',frame);target=Control('Edit',wrapper)
    path=class_path(target,7)
    assert [p['class_name'] for p in path] == ['AppWindow','Frame','Edit']
    assert resolve_class_path(root,path,7) is target
    Control('Edit',wrapper)
    with pytest.raises(AssertionError): resolve_class_path(root,path,7)
    with pytest.raises(ValueError): class_path(window,7)
    with pytest.raises(ValueError): class_path(wrapper,7)


def test_api_guard_validation_and_export(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/status').json()['active_id'] is None
        assert client.get('/api/sessions').json() == []
        assert client.post('/api/sessions',json={'url':'file:///etc/passwd'}).status_code == 422
        assert client.post('/api/sessions',json={'url':'https://user:secret@example.com'}).status_code == 422
        assert client.post('/api/sessions',headers={'Origin':'https://evil.example'},json={}).status_code == 403
        assert client.get('/api/sessions/not-a-session').status_code == 404
        if sys.platform != 'win32':
            assert client.post('/api/sessions',json={'backend':'windows','process_id':7}).status_code == 422


async def test_thread_compat_preserves_context_arguments_and_errors():
    import contextvars
    import threading
    from web_test_trans.compat import run_in_thread
    value = contextvars.ContextVar('test_value',default='unset')
    value.set('from-event-loop')
    owner = threading.get_ident()
    def read_context(prefix, *, suffix):
        assert threading.get_ident() != owner
        return prefix + value.get() + suffix
    assert await run_in_thread(read_context,'[',suffix=']') == '[from-event-loop]'
    def fail():
        raise ValueError('worker failure')
    with pytest.raises(ValueError,match='worker failure'):
        await run_in_thread(fail)


def test_concurrent_start_uses_server_event_loop(tmp_path,monkeypatch):
    import asyncio
    import httpx
    import web_test_trans.app as app_module

    class FakeRecorder:
        def __init__(self,store,session):
            self.store,self.session=store,session
        async def start(self,**kwargs):
            await asyncio.sleep(.05)
            self.session['status']='recording'
            self.store.save(self.session)
        async def stop(self):
            self.session['status']='stopped'
            self.store.save(self.session)

    monkeypatch.setattr(app_module,'WebRecorder',FakeRecorder)
    # Build outside the server's event loop, as the CLI actually does.
    app=app_module.create_app(tmp_path)
    async def requests():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
                responses=await asyncio.gather(*[
                    client.post('/api/sessions',json={'url':'https://example.com','name':name})
                    for name in ('first','second')
                ])
                assert sorted(response.status_code for response in responses)==[200,409]
                active=(await client.get('/api/status')).json()['active_id']
                assert (await client.post('/api/sessions/'+active+'/stop')).status_code==200
    asyncio.run(requests())


def test_cli_serves_local_api(tmp_path):
    import socket
    import subprocess
    import time
    import urllib.error
    import urllib.request
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    process=subprocess.Popen([sys.executable,'-m','web_test_trans','--port',str(port),
                              '--data-dir',str(tmp_path/'cli-data')],
                             stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline and process.poll() is None:
            try:
                with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/api/status',timeout=1) as response:
                    status=json.load(response)
                assert status['active_id'] is None
                return
            except (urllib.error.URLError,TimeoutError):
                time.sleep(.05)
        raise AssertionError('CLI failed to serve the local API')
    finally:
        process.terminate()
        try:
            output,_=process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output,_=process.communicate(timeout=5)
        print(output)


@pytest.mark.parametrize('backend',['web','windows'])
def test_exports_import_without_server_and_support_python38(tmp_path,backend):
    import os
    import subprocess
    from packaging.requirements import Requirement
    from packaging.markers import default_environment
    store = Store(tmp_path/'data')
    session = store.create('compatibility',backend,url='https://example.com',toast_window_ms=1000,http_auth=False)
    target = tmp_path/'export'
    target.mkdir()
    with zipfile.ZipFile(BytesIO(export_bundle(session,store.components))) as archive:
        requirements = archive.read('requirements.txt').decode().splitlines()
        archive.extractall(target)
    if backend == 'web':
        env38 = dict(default_environment(),python_version='3.8',python_full_version='3.8.20')
        matching = [Requirement(line) for line in requirements if Requirement(line).name == 'playwright'
                    and Requirement(line).marker.evaluate(env38)]
        assert len(matching) == 1 and '1.48.0' in matching[0].specifier
        assert '1.49.0' not in matching[0].specifier
    # Import the standalone generated files, including evaluated dataclass hints.
    env = dict(os.environ)
    env.pop('PYTHONPATH',None)
    result = subprocess.run([os.getenv('WTT_REPLAY_PYTHON',sys.executable),'-c','import test_recording'],
                            cwd=target,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode == 0,result.stderr


@pytest.mark.skipif(sys.platform != 'win32',reason='Windows UIA dependencies only')
def test_windows_optional_dependencies_import():
    import importlib.util
    import os
    # The browser-only CI job deliberately does not install the Windows extra.
    if not os.getenv('WTT_REQUIRE_WINDOWS_DEPS') and importlib.util.find_spec('uiautomation') is None:
        pytest.skip('Windows extra not installed in browser-only environment')
    from web_test_trans.windows_runtime import WindowsComponents
    components = WindowsComponents({})
    assert components.auto.GetRootControl() is not None


def test_generated_toast_api_and_secret_placeholder(tmp_path):
    store=Store(tmp_path)
    button=store.register('button',{'backend':'web','css':'main > div > button','frames':[]})
    toast=store.register('toast',{'backend':'web','css':'main > aside > div.toast','frames':[]})
    password=store.register('input',{'backend':'web','css':'main > form > input.password','frames':[]})
    session=store.create('test','web',url='https://example.com',http_auth=True,toast_window_ms=5000)
    store.append(session,{'type':'page_open','page':'page_0'})
    store.append(session,{'type':'navigation','page':'page_0','url':session['url']})
    store.append(session,{'type':'fill','component':password,'page':'page_0','secret_env':'WTT_SECRET_PASSWORD'})
    click=store.append(session,{'type':'click','component':button,'page':'page_0'})
    store.append(session,{'type':'toast','component':toast,'page':'page_0','text':'保存成功','after_action':click['id']})
    code=generate(session,store.components)
    assert 'assert_after_click' in code and 'WTT_SECRET_PASSWORD' in code
    assert make_config(session)['browser']['http_credentials']['password']=='${WTT_HTTP_PASSWORD}'
    assert 'get_by_text' not in code and 'has_text' not in code
    compile(code,'recording.py','exec')
    with zipfile.ZipFile(BytesIO(export_bundle(session,store.components))) as archive:
        assert 'runtime.py' in archive.namelist()
        assert 'config.yml' in archive.namelist()
        assert len(json.loads(archive.read('components.json'))) == 3


async def test_toast_listener_is_separate_process_and_correlates(tmp_path):
    import os
    from web_test_trans.toast_listener import ToastListenerProcess
    store=Store(tmp_path)
    session=store.create('listener','web',toast_window_ms=1000)
    listener=ToastListenerProcess(store,session)
    await listener.start()
    assert session['toast_listener_pid'] != os.getpid()
    listener.action({'id':'click','at':1000,'page':'page_0'})
    listener.toast({'type':'toast','at':1100,'page':'page_0','component':'toast_key','text':'ok'})
    await listener.stop()
    assert session['events'][0]['after_action']=='click'
