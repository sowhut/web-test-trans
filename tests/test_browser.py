import asyncio
import json
import os
import subprocess
import sys
import zipfile
from io import BytesIO

from web_test_trans.exporter import export_bundle
from web_test_trans.store import Store, validate_locator
from web_test_trans.web_recorder import WebRecorder


async def recorder_for(tmp_path, url, credentials=None):
    store=Store(tmp_path / 'data')
    session=store.create('integration','web',url=url,toast_window_ms=2000,http_auth=False)
    recorder=WebRecorder(store,session)
    await recorder.start(headless=True,credentials=credentials)
    return store,session,recorder


async def test_record_export_replay_keys_mouse_frame_toasts_secret(tmp_path,site):
    store,session,recorder=await recorder_for(tmp_path,site+'/demo')
    try:
        page=recorder.context.pages[0]
        await page.locator('.username').click()
        await page.locator('.username').press_sequentially('hello')
        await page.locator('.username').press('Tab')
        await page.locator('.password').press_sequentially('never-store-this')
        await page.locator('.plan').select_option('pro')
        await page.locator('.agree').check()
        await page.locator('.save-button').click()
        await page.wait_for_timeout(100)
        await page.locator('.flash-button').click()
        await page.wait_for_timeout(250)
        await page.locator('.double-button').dblclick()
        await page.wait_for_timeout(150)
        frame=page.frame_locator('.demo-frame')
        await frame.locator('.frame-input').fill('in a frame')
        await frame.locator('.frame-save').click()
        await page.wait_for_timeout(150)
        await page.locator('.drag-source').drag_to(page.locator('.drop-zone'))
        await page.wait_for_timeout(150)
    finally:
        await recorder.stop()
    encoded=json.dumps(session,ensure_ascii=False)
    assert 'never-store-this' not in encoded
    assert not session['warnings'], session['warnings']
    types={e['type'] for e in session['events']}
    assert {'fill','press','check','select','click','dblclick','toast','drag','key_event','mouse_event'} <= types
    toasts=[e for e in session['events'] if e['type']=='toast']
    assert any(e['text']=='短暂提示' and e.get('after_action') for e in toasts)
    assert any(store.components[e['component']]['locator'].get('frames') for e in session['events'] if e.get('component'))
    for definition in store.components.values(): validate_locator(definition['locator'])
    target=tmp_path/'export';target.mkdir()
    with zipfile.ZipFile(BytesIO(export_bundle(session,store.components))) as archive: archive.extractall(target)
    env={**os.environ,'WTT_HEADLESS':'1'}
    env.update({e['secret_env']:'replacement-secret' for e in session['events'] if e.get('secret_env')})
    proc=await asyncio.create_subprocess_exec(sys.executable,str(target/'test_recording.py'),env=env,
                                              stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await asyncio.wait_for(proc.communicate(),60)
    assert proc.returncode==0, (stdout+stderr).decode()


async def test_nested_cross_origin_frames(tmp_path,site):
    store,session,recorder=await recorder_for(tmp_path,site+'/cross')
    try:
        page=recorder.context.pages[0]
        target=page.frame_locator('.outer').frame_locator('.inner').locator('.frame-input')
        await target.fill('nested')
        await page.wait_for_timeout(100)
    finally:
        await recorder.stop()
    fills=[e for e in session['events'] if e['type']=='fill']
    assert len(fills)==1
    locator=store.components[fills[0]['component']]['locator']
    assert len(locator['frames'])==2
    validate_locator(locator)


async def test_http_auth_detection_and_credentials(tmp_path,site):
    store,session,recorder=await recorder_for(tmp_path,site+'/auth')
    try:
        await recorder.context.pages[0].wait_for_timeout(100)
        assert any(e['type']=='auth_challenge' for e in session['events']), session
        await recorder.authenticate('user','secret')
        await recorder.context.pages[-1].locator('.username').fill('authenticated')
        await recorder.context.pages[-1].wait_for_timeout(100)
    finally:
        await recorder.stop()
    assert session['http_auth']
    assert '"password": "secret"' not in json.dumps(session)
    target=tmp_path/'auth-export';target.mkdir()
    with zipfile.ZipFile(BytesIO(export_bundle(session,store.components))) as archive: archive.extractall(target)
    import yaml
    config=yaml.safe_load((target/'config.yml').read_text())
    assert config['browser']['http_credentials']['username']=='${WTT_HTTP_USERNAME}'
    env={**os.environ,'WTT_HEADLESS':'1','WTT_HTTP_USERNAME':'user','WTT_HTTP_PASSWORD':'secret'}
    proc=await asyncio.create_subprocess_exec(sys.executable,str(target/'test_recording.py'),env=env,
                                              stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await asyncio.wait_for(proc.communicate(),40)
    assert proc.returncode==0,(stdout+stderr).decode()


async def test_duplicate_div_never_falls_back_to_position(tmp_path,site):
    store,session,recorder=await recorder_for(tmp_path,site+'/demo')
    try:
        page=recorder.context.pages[0]
        result=await page.evaluate('''() => {
          const parent=document.createElement('section');parent.className='duplicate-list';
          const a=document.createElement('div'), b=document.createElement('div');
          a.className=b.className='item';parent.append(a,b);document.querySelector('main').append(parent);
          let rejected=false;try {window.__wtt.selector(b);}catch {rejected=true;}
          b.dataset.testid='second-semantic-item';
          return {rejected,css:window.__wtt.selector(b)};
        }''')
        assert result['rejected']
        assert 'nth-' not in result['css'] and 'data-testid' in result['css']
        assert len(result['css'].split(' > '))>=3
    finally:
        await recorder.stop()


async def test_popup_export_replays_without_ordinal_page_selection(tmp_path,site):
    store,session,recorder=await recorder_for(tmp_path,site+'/demo')
    try:
        page=recorder.context.pages[0]
        async with page.expect_popup() as popup:
            await page.locator('.new-window').click()
        child=await popup.value
        await child.locator('.frame-input').fill('popup')
        await child.locator('.frame-save').click()
        await child.wait_for_timeout(100)
        await child.close()
    finally:
        await recorder.stop()
    target=tmp_path/'popup-export';target.mkdir()
    with zipfile.ZipFile(BytesIO(export_bundle(session,store.components))) as archive: archive.extractall(target)
    proc=await asyncio.create_subprocess_exec(sys.executable,str(target/'test_recording.py'),env={**os.environ,'WTT_HEADLESS':'1'},
                                              stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await asyncio.wait_for(proc.communicate(),40)
    assert proc.returncode==0,(stdout+stderr).decode()


def test_toast_assertion_remembers_short_lived_toast_and_fails_missing(site):
    import pytest
    from playwright.sync_api import sync_playwright
    from web_test_trans.runtime import ComponentLibrary,ToastAssertions,ToastExpectation
    library=ComponentLibrary({'toast':{'kind':'toast','locator':{'css':'main.demo-app > div.toast-container > div.toast','frames':[]}}})
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page();page.goto(site+'/demo')
        toasts=ToastAssertions(library)
        def click():
            page.locator('.flash-button').click()
            page.wait_for_timeout(350)
        toasts.assert_after_click(page,click,ToastExpectation('toast','短暂提示'),timeout_ms=600)
        with pytest.raises(AssertionError):
            toasts.assert_after_click(page,lambda:None,ToastExpectation('toast','missing'),timeout_ms=100)
        browser.close()
