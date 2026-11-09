"""Run EventDesk's existing HTTP handlers without opening a socket.

The hosted entrypoint owns durable transactions. It must inspect ``status`` and
roll back failed requests, including errors caught by the original handlers.
"""
from email.message import Message
import io
import json
import re


HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class _ResponseCapture:
    def send_response(self, code, message=None):
        self.response_status = int(code)

    def send_header(self, keyword, value):
        self.response_headers.append((str(keyword), str(value)))

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        # The Worker owns access logs; request paths may contain private QR data.
        pass

    def stream_projector_events(self):
        """Send a finite snapshot; EventSource reconnects instead of blocking."""
        state = self.app_module.get_projector_state()
        if state['event_name'] == 'Company Party':
            state['event_name'] = self.config.get('event_name', 'Company Party')
        payload = json.dumps(state, ensure_ascii=False, separators=(',', ':'))
        event = f"retry: 3000\nid: {state['sequence']}\nevent: draw-state\ndata: {payload}\n\n"
        self.send_bytes(event.encode('utf-8'), 'text/event-stream; charset=utf-8')


def _request_headers(headers, body_length):
    result = Message()
    pairs = headers.items() if hasattr(headers, 'items') else headers
    for name, value in pairs:
        if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
            raise ValueError('Invalid request header.')
        if not isinstance(value, str) or any(char in value for char in '\r\n\0'):
            raise ValueError('Invalid request header.')
        if name.lower() != 'content-length':
            result[name] = value
    # Fetch has already framed the body, so stale client lengths are not trusted.
    result['Content-Length'] = str(body_length)
    return result


def _make_handler(app_module, method, path, headers, body, client_ip, config):
    handler_type = type('HostedPartyHandler', (_ResponseCapture, app_module.PartyHandler), {})
    handler = object.__new__(handler_type)
    handler.app_module = app_module
    handler.config = config if config is not None else app_module.load_config()
    handler.command = method
    handler.path = path
    handler.headers = headers
    handler.client_address = (client_ip, 0)
    handler.request_version = 'HTTP/1.1'
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.response_status = 500
    handler.response_headers = []
    return handler


def dispatch(app_module, method, path, headers, body=b'', client_ip='127.0.0.1', config=None):
    """Return ``status``, ordered header pairs, and body bytes for one request.

    ``config`` can be supplied directly or through the patched ``load_config``.
    Input headers may be a mapping, an email Message, or ordered name/value pairs.
    Multiple response headers with the same name remain separate.
    """
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise TypeError('The request body must be bytes.')
    body = bytes(body)
    handler = _make_handler(app_module, method, path, Message(), body, client_ip, config)
    try:
        if not isinstance(method, str) or not isinstance(path, str):
            raise ValueError('Invalid request.')
        if not path.startswith('/') or path.startswith('//') or any(char in path for char in '\r\n\0'):
            raise ValueError('Invalid request path.')
        handler.headers = _request_headers(headers, len(body))
        if len(body) > app_module.MAX_BODY:
            handler.fail('Request is too large.', 413)
        elif method == 'GET':
            handler.do_GET()
        elif method == 'POST':
            handler.do_POST()
        else:
            handler.respond({'error': 'Method not allowed.'}, 405, {'Allow': 'GET, POST'})
    except ValueError as error:
        handler.fail(str(error), 400)
    return {
        'status': handler.response_status,
        'headers': list(handler.response_headers),
        'body': handler.wfile.getvalue(),
    }
