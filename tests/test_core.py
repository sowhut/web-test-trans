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
        assert client.post('/api/sessions',json={'url':'file:///etc/passwd'}).status_code == 422
        assert client.post('/api/sessions',json={'url':'https://user:secret@example.com'}).status_code == 422
        assert client.post('/api/sessions',headers={'Origin':'https://evil.example'},json={}).status_code == 403
        assert client.get('/api/sessions/not-a-session').status_code == 404
        if sys.platform != 'win32':
            assert client.post('/api/sessions',json={'backend':'windows','process_id':7}).status_code == 422


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
