import io
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml

RAW = {'mouse_event', 'key_event', 'auth_challenge'}


def normalized_events(session):
    events = []
    for event in sorted(session['events'], key=lambda e: e['at']):
        if event['type'] in RAW or event.get('superseded'):
            continue
        # Consecutive input updates become one fill, preserving intervening actions.
        if events and event['type'] == 'fill' and events[-1]['type'] == 'fill' and event.get('component') == events[-1].get('component'):
            events[-1] = event
        else:
            events.append(event)
    return events


def generate(session, registry):
    if session['backend'] == 'windows':
        return generate_windows(session)
    lines = ['import os', 'from pathlib import Path', 'from playwright.sync_api import sync_playwright',
             'from runtime import ComponentLibrary, ToastAssertions, ToastExpectation, wait_for_new_page', '',
             'from configuration import load_config', '',
             'BASE = Path(__file__).parent', '', 'def run():',
             '    components = ComponentLibrary(BASE / "components.json")',
             '    toasts = ToastAssertions(components)', '    with sync_playwright() as pw:',
             '        config = load_config(BASE / "config.yml")',
             '        options = dict(config["browser"])',
             '        headless = options.pop("headless", False) or os.getenv("WTT_HEADLESS") == "1"',
             '        browser = pw.chromium.launch(headless=headless)']
    lines += ['        context = browser.new_context(**options)', '        pages = {}', '        try:']
    body = []
    events = normalized_events(session)
    expectations = {}
    for event in events:
        if event['type'] == 'toast' and event.get('after_action'):
            expectations.setdefault(event['after_action'], []).append(event)
    opened = set()
    last_action = {}
    for event in events:
        kind, page_id = event['type'], event.get('page', 'page_0')
        p = f'pages[{page_id!r}]'
        ref = event.get('component')
        definition = registry.get(ref, {})
        getter = definition.get('kind') if definition.get('kind') in ('button', 'input', 'frame') else 'get'
        component = f'components.{getter}({ref!r})'
        expression = None
        if kind == 'page_open':
            if event.get('opener') and opened:
                body.append(f'{p} = wait_for_new_page(context, list(pages.values()))')
            else:
                body.append(f'{p} = context.new_page()')
            opened.add(page_id)
        elif kind == 'navigation':
            if page_id not in opened:
                body.append(f'{p} = context.new_page()')
                opened.add(page_id)
            action = last_action.get(page_id)
            if action and 0 <= event['at'] - action['at'] < 5000:
                body.append(f'{p}.wait_for_url({event["url"]!r})')
            else:
                address = 'config["recording"]["url"]' if event['url'] == session['url'] else repr(event['url'])
                body.append(f'{p}.goto({address})')
        elif kind in ('click','dblclick'):
            expression = f'{component}.{kind}({p}, button={event.get("button", "left")!r}, modifiers={event.get("modifiers", [])!r})'
        elif kind == 'fill':
            value = f'config["parameters"][{event["secret_env"]!r}]' if event.get('secret_env') else repr(event.get('value', ''))
            expression = f'{component}.fill({p}, {value})'
        elif kind == 'press':
            expression = f'{component}.press({p}, {event["key"]!r})'
        elif kind == 'check':
            expression = f'{component}.check({p}, {event["checked"]!r})'
        elif kind == 'select':
            expression = f'{component}.select({p}, {event["value"]!r})'
        elif kind == 'hover':
            expression = f'{component}.hover({p})'
        elif kind == 'wheel':
            expression = f'{component}.wheel({p}, {event["dx"]!r}, {event["dy"]!r})'
        elif kind == 'drag':
            expression = f'{component}.drag_to({p}, components.get({event["destination"]!r}))'
        elif kind == 'toast' and not event.get('after_action'):
            expression = f'toasts.assert_visible({p}, ToastExpectation({ref!r}, {event["text"]!r}))'
        elif kind == 'page_close':
            if page_id in opened:
                body.append(f'{p}.close()')
        if expression:
            if event['id'] in expectations:
                expected = ', '.join(f'ToastExpectation({e["component"]!r}, {e["text"]!r})' for e in expectations[event['id']])
                expression = f'toasts.assert_after_click({p}, lambda: {expression}, [{expected}], timeout_ms={session["toast_window_ms"]})'
            body.append(expression)
        if kind in ('click','dblclick','press','check','select'):
            last_action[page_id] = event
    lines.extend('            ' + line for line in (body or ['pass']))
    lines += ['        finally:', '            context.close()', '            browser.close()', '',
              'if __name__ == "__main__":', '    run()', '']
    return '\n'.join(lines)


def generate_windows(session):
    lines = ['import os', 'from pathlib import Path',
             'from windows_runtime import WindowsComponents, WindowsToastAssertions, NativeToastExpectation', '',
             'from configuration import load_config', '',
             'def run():', '    components = WindowsComponents(Path(__file__).with_name("components.json"))',
             '    toasts = WindowsToastAssertions(components)',
             '    config = load_config(Path(__file__).with_name("config.yml"))',
             '    components.parameters = config["parameters"]',
             '    # Set WTT_PROCESS_ID to the running target application process ID.',
             '    components.process_id = int(config["windows"]["process_id"])']
    events = normalized_events(session)
    groups = {}
    for e in events:
        if e['type'] == 'toast' and e.get('after_action'):
            groups.setdefault(e['after_action'], []).append(e)
    for e in events:
        kind = e['type']
        if kind == 'toast':
            if not e.get('after_action'):
                lines.append(f'    toasts.assert_visible(NativeToastExpectation({e["component"]!r}, {e["text"]!r}))')
            continue
        if kind not in ('click', 'dblclick', 'key_down', 'key_up', 'wheel', 'drag', 'fill_secret'):
            continue
        payload = {k:v for k,v in e.items() if k in ('button','key','dx','dy','destination','secret_env')}
        expression = f'components.perform({e["component"]!r}, {kind!r}, {payload!r})'
        if e['id'] in groups:
            expected = ', '.join(f'NativeToastExpectation({t["component"]!r}, {t["text"]!r})' for t in groups[e['id']])
            expression = f'toasts.assert_after_click(lambda: {expression}, [{expected}], timeout_ms={session["toast_window_ms"]})'
        lines.append('    ' + expression)
    lines += ['', 'if __name__ == "__main__":', '    run()', '']
    return '\n'.join(lines)


def export_bundle(session, registry):
    refs = {e[k] for e in session['events'] for k in ('component','destination') if isinstance(e.get(k), str)}
    definitions = {k: v for k,v in registry.items() if k in refs}
    code = generate(session, registry)
    compile(code, 'test_recording.py', 'exec')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('test_recording.py', code)
        archive.writestr('components.json', json.dumps(definitions, ensure_ascii=False, indent=2))
        archive.writestr('recording.json', json.dumps(session, ensure_ascii=False, indent=2))
        archive.writestr('config.yml',yaml.safe_dump(make_config(session),allow_unicode=True,sort_keys=False))
        archive.write(Path(__file__).with_name('configuration.py'),'configuration.py')
        runtime = 'runtime.py' if session['backend'] == 'web' else 'windows_runtime.py'
        archive.write(Path(__file__).with_name(runtime), runtime)
        # Keep both markers so an export created on 3.12 can be installed on 3.8.
        requirements = ("playwright==1.48.0; python_version < '3.9'\n"
                        "playwright>=1.51,<2; python_version >= '3.9'\n"
                        if session['backend'] == 'web' else 'uiautomation>=2.0.20\npynput>=1.7.7\n') + 'PyYAML>=6,<7\n'
        archive.writestr('requirements.txt', requirements)
        secrets = sorted({e['secret_env'] for e in session['events'] if e.get('secret_env')})
        if make_config(session).get('browser',{}).get('http_credentials'):
            secrets += ['WTT_HTTP_USERNAME','WTT_HTTP_PASSWORD']
        if session['backend'] == 'windows':
            secrets += ['WTT_PROCESS_ID']
        archive.writestr('.env.example', '\n'.join(f'{key}=' for key in secrets) + '\n')
        archive.writestr('README.txt', '支持 Python 3.8+。安装：python -m pip install -r requirements.txt\n网页需运行：python -m playwright install chromium\n'
                          'Python 3.8 自动安装 Playwright 1.48.0；其他 Python 版本按 requirements.txt 选择兼容版本。\n'
                          'config.yml 保存参数，可直接修改；推荐设置 .env.example 所列的环境变量（不会自动加载 .env），运行 python test_recording.py。\n'
                          'Windows 原生回放必须在 Windows 交互式桌面运行并先启动目标程序。\n'
                          '组件可通过 components.get(key) 复用；toast 文案仅用于断言，不用于定位。\n'
                          '录制包含业务输入和 URL，请在分享前检查 recording.json。\n')
    return buffer.getvalue()


def make_config(session):
    parameters={e['secret_env']:'${'+e['secret_env']+'}' for e in session['events'] if e.get('secret_env')}
    config={'recording':{'backend':session['backend'],'url':session.get('url','')},'parameters':parameters}
    if session['backend']=='web':
        config['browser']={'headless':False,'viewport':{'width':1360,'height':900}}
        if session.get('http_auth') or any(e['type']=='auth_challenge' and e.get('status')==401 for e in session['events']):
            url=urlsplit(session['url'])
            config['browser']['http_credentials']={'origin':session.get('http_auth_origin') or f'{url.scheme}://{url.netloc}',
                'username':'${WTT_HTTP_USERNAME}','password':'${WTT_HTTP_PASSWORD}'}
    else:
        config['windows']={'process_id':'${WTT_PROCESS_ID}'}
    return config
