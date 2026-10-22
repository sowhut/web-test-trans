import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / 'web_test_trans' / 'static'


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?',1)[0]
            if path == '/auth' and self.headers.get('Authorization') != 'Basic ' + base64.b64encode(b'user:secret').decode():
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Test"')
                self.end_headers()
                self.wfile.write(b'Authentication required')
                return
            if path in ('/demo', '/auth'):
                html = (STATIC / 'demo.html').read_bytes()
            elif path == '/demo/frame':
                html = (STATIC / 'frame.html').read_bytes()
            elif path == '/cross':
                html = f'<html><body><main><div class="frames"><iframe class="outer" src="http://localhost:{self.server.server_port}/nested"></iframe></div></main></body></html>'.encode()
            elif path == '/nested':
                html = b'<html><body><main><section><iframe class="inner" src="/demo/frame"></iframe></section></main></body></html>'
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join()
