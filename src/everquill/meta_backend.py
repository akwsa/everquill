"""Offline-first Meta Responses adapter; transport is explicitly injected.

No credential discovery or network default. Transport accepts keyword arguments
url, payload, headers, timeout, cancel_event and returns a decoded response dict
or an iterable of complete SSE frames (str/bytes). It must enforce its timeout
and observe cancel_event; Python cannot interrupt a blocked injected operation.
Cancellation never proves server termination. No local tools are dispatched.

Mixed commentary and tool calls retain their correlation IDs in neutral replies.
ToolResult inputs support same-instance correlation or explicit recovery IDs
bound to a persisted response ID. The CLI permits only bounded admitted read_file
calls, with fresh recovery-execution and outgoing-result consent. Verified IDs
and pending read calls/results can be persisted by the controller for restart
continuation. start_session is unsupported because
Meta continuation begins with the first actual response, after context consent.
The factory opts into standard-library HTTPS; direct instances remain injected.
"""

from copy import deepcopy
import json
import math
import time
from threading import Event

from everquill.reasoning import BackendCapabilities, CancellationStatus
from everquill.reply import BackendReply


URL = 'https://api.meta.ai/v1/responses'
MODEL = 'muse-spark-1.3'


class ProviderStatusError(RuntimeError):
    def __init__(self, status):
        self.status = status
        self.category = ('authentication' if status == 401 else
                         'authorization' if status == 403 else
                         'rate_limit' if status == 429 else
                         'provider' if status >= 500 else 'request')
        super().__init__('backend ' + self.category + ' failure')


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = item
        return result

    def constant(value):
        raise ValueError('nonfinite JSON value')

    if not isinstance(value, str):
        raise ValueError('invalid JSON payload')
    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def https_transport(*, url, payload, headers, timeout, cancel_event):
    """Bounded non-streaming HTTPS; no redirects, retries or credential discovery."""
    import http.client
    if url != URL:
        raise ValueError('unsupported backend URL')
    if not headers.get('Authorization'):
        raise RuntimeError('backend authentication required: MODEL_API_KEY')
    if cancel_event.is_set():
        raise RuntimeError('backend cancelled')
    connection = http.client.HTTPSConnection('api.meta.ai', timeout=timeout)
    try:
        body = json.dumps({**payload, 'stream': False}, allow_nan=False).encode('utf-8')
        connection.request('POST', '/v1/responses', body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ProviderStatusError(response.status)
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('backend response too large')
        return _json(raw.decode('utf-8'))
    finally:
        connection.close()


class MetaModelBackend:
    capabilities = BackendCapabilities(True, True, True, False, None)

    def __init__(self, transport=None, *, api_key=None, timeout=90):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid backend timeout')
        if api_key is not None and (not isinstance(api_key, str) or not api_key or '\r' in api_key or '\n' in api_key):
            raise ValueError('invalid backend credential')
        self._reply = None
        self._response_id = None
        self._call_ids = ()
        self._transport = transport
        self._api_key = api_key
        self._timeout = timeout
        self._cancel_event = Event()
        self._payload = None
        self._attempted = False
        self._closed = False

    def start_session(self, project_label):
        raise NotImplementedError('Meta session creation requires a response ID')

    def send_message(self, messages, tools=None, metadata=None):
        if self._closed or self._cancel_event.is_set() or (self._payload is not None and self._reply is None):
            raise RuntimeError('backend is not available')
        from everquill.reply import ToolResult
        if metadata is not None and (not isinstance(metadata, dict)
                                     or set(metadata) - {'session_id', 'pending_call_ids'}):
            raise ValueError('invalid backend metadata')
        restored_ids = (metadata or {}).get('pending_call_ids')
        if restored_ids is not None:
            if (self._payload is not None or self._response_id is not None
                    or not isinstance((metadata or {}).get('session_id'), str)
                    or not metadata['session_id']
                    or not isinstance(restored_ids, list) or not restored_ids
                    or any(not isinstance(value, str) or not value for value in restored_ids)
                    or len(set(restored_ids)) != len(restored_ids)
                    or not isinstance(messages, list) or not messages
                    or not all(isinstance(message, ToolResult) for message in messages)):
                raise ValueError('invalid pending call recovery')
        if isinstance(messages, list) and messages and all(isinstance(m, ToolResult) for m in messages):
            ids = [m.call_id for m in messages]
            expected_ids = restored_ids if restored_ids is not None else self._call_ids
            if len(set(ids)) != len(ids) or set(ids) != set(expected_ids):
                raise ValueError('tool results must match pending calls')
            messages = [{'type': 'function_call_output', 'call_id': m.call_id, 'output': m.output} for m in messages]
        elif not isinstance(messages, list) or not messages or any(
            not isinstance(m, dict) or set(m) != {'role', 'content'}
            or m['role'] not in ('system', 'developer', 'user', 'assistant')
            or not isinstance(m['content'], str) or not m['content'].strip()
            for m in messages
        ):
            raise ValueError('invalid backend messages')
        if tools is not None and (not isinstance(tools, list) or any(
            not isinstance(t, dict) or t.get('type') != 'function'
            or not isinstance(t.get('name'), str) or not t['name']
            or not isinstance(t.get('parameters'), dict)
            or set(t) - {'type', 'name', 'description', 'parameters', 'strict'}
            for t in tools
        )):
            raise ValueError('invalid backend tools')
        session_id = (metadata or {}).get('session_id', self._response_id)
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise ValueError('invalid backend session ID')
        self._reply = None
        self._attempted = False
        self._payload = {'model': MODEL, 'input': deepcopy(messages), 'stream': True}
        if tools is not None:
            self._payload['tools'] = deepcopy(tools)
        if session_id is not None:
            self._payload['previous_response_id'] = session_id

    def stream_response(self):
        if self._payload is None or self._closed:
            raise RuntimeError('no pending backend request')
        if self._reply is not None:
            return self._reply
        if self._attempted:
            raise RuntimeError('backend request already consumed')
        if self._cancel_event.is_set():
            return BackendReply('cancelled')
        if self._transport is None:
            if not self._api_key:
                raise RuntimeError('backend authentication required: MODEL_API_KEY')
            raise RuntimeError('live transport is disabled; inject an offline transport')
        headers = {'Content-Type': 'application/json'}
        if self._api_key:
            headers['Authorization'] = 'Bearer ' + self._api_key
        self._attempted = True
        self._deadline = time.monotonic() + self._timeout
        try:
            response = self._transport(url=URL, payload=deepcopy(self._payload),
                                       headers=headers, timeout=self._timeout,
                                       cancel_event=self._cancel_event)
            if self._cancel_event.is_set():
                return BackendReply('cancelled')
            if time.monotonic() >= self._deadline:
                return BackendReply('timeout')
            if not isinstance(response, dict):
                response = self._sse(response)
            if self._cancel_event.is_set():
                return BackendReply('cancelled')
            reply = self._decode(response)
            if time.monotonic() >= self._deadline:
                return BackendReply('timeout')
            self._reply = reply
            return reply
        except TimeoutError:
            return BackendReply('cancelled' if self._cancel_event.is_set() else 'timeout')
        except Exception as exc:
            status = getattr(exc, 'status_code', getattr(exc, 'status', None))
            if isinstance(exc, RuntimeError) and str(exc) == 'backend authentication required: MODEL_API_KEY':
                failure = RuntimeError('backend authentication required: MODEL_API_KEY')
            elif isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
                failure = ProviderStatusError(status)
            else:
                failure = RuntimeError('backend response or transport failed')
        raise failure from None

    def _sse(self, frames):
        final = None
        response_id = None
        allowed = {'response.created', 'response.in_progress',
                   'response.output_item.added', 'response.output_item.done',
                   'response.content_part.added', 'response.content_part.done',
                   'response.output_text.delta', 'response.output_text.done',
                   'response.function_call_arguments.delta',
                   'response.function_call_arguments.done', 'response.completed'}
        for frame in frames:
            if self._cancel_event.is_set():
                return None
            if time.monotonic() >= self._deadline:
                raise TimeoutError()
            if isinstance(frame, bytes):
                frame = frame.decode('utf-8', errors='strict')
            if not isinstance(frame, str):
                raise ValueError('invalid SSE frame')
            frame = frame.replace('\r\n', '\n')
            if not frame.endswith('\n\n'):
                raise ValueError('partial SSE frame')
            lines = frame[:-2].split('\n')
            data = []
            event_name = None
            for line in lines:
                if line.startswith(':'):
                    continue
                field, separator, value = line.partition(':')
                if not separator or field not in ('data', 'event', 'id', 'retry'):
                    raise ValueError('invalid SSE field')
                value = value.removeprefix(' ')
                if field == 'data':
                    data.append(value)
                elif field == 'event':
                    event_name = value
            if not data and all(line.startswith(':') for line in lines):
                continue
            raw = '\n'.join(data)
            if raw == '[DONE]':
                if final is None:
                    raise ValueError('premature SSE completion')
                continue
            event = _json(raw)
            if not isinstance(event, dict) or event.get('type') not in allowed or final is not None:
                raise ValueError('invalid SSE event')
            if event_name is not None and event_name != event['type']:
                raise ValueError('SSE event mismatch')
            if event['type'] in ('response.created', 'response.in_progress', 'response.completed') and not isinstance(event.get('response'), dict):
                raise ValueError('missing SSE response')
            if event['type'] in ('response.output_item.added', 'response.output_item.done') and not isinstance(event.get('item'), dict):
                raise ValueError('missing SSE item')
            if event['type'] in ('response.content_part.added', 'response.content_part.done') and not isinstance(event.get('part'), dict):
                raise ValueError('missing SSE content part')
            if 'response' in event:
                current_id = event['response'].get('id')
                if not isinstance(current_id, str) or not current_id or (response_id is not None and current_id != response_id):
                    raise ValueError('SSE response mismatch')
                response_id = current_id
            if event['type'].endswith('.delta') and not isinstance(event.get('delta'), str):
                raise ValueError('invalid SSE delta')
            if event['type'] == 'response.completed':
                final = event['response']
        if final is None:
            raise ValueError('missing completed response')
        return final

    def _decode(self, response):
        from everquill.reply import ToolCallRequest
        if not isinstance(response, dict) or response.get('status') != 'completed' or not isinstance(response.get('id'), str) or not response['id']:
            raise ValueError('invalid backend response')
        output = response.get('output')
        if not isinstance(output, list) or not output:
            raise ValueError('empty backend output')
        text, calls, call_ids = [], [], []
        for item in output:
            if not isinstance(item, dict):
                raise ValueError('invalid backend output')
            if item.get('type') == 'function_call':
                name, call_id = item.get('name'), item.get('call_id')
                if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id or call_id in call_ids:
                    raise ValueError('invalid function call')
                arguments = _json(item['arguments'])
                if not isinstance(arguments, dict):
                    raise ValueError('invalid function arguments')
                calls.append(ToolCallRequest(name, arguments, call_id))
                call_ids.append(call_id)
                continue
            if item.get('type') != 'message' or item.get('role') != 'assistant' or not isinstance(item.get('content'), list) or not item['content']:
                raise ValueError('unsupported backend output')
            for part in item['content']:
                if not isinstance(part, dict) or part.get('type') != 'output_text' or not isinstance(part.get('text'), str) or not part['text'].strip():
                    raise ValueError('unsupported backend content')
                text.append(part['text'])
        reply = BackendReply('tool_request', ''.join(text), tuple(calls), response['id']) if calls else BackendReply('complete', ''.join(text), response_id=response['id'])
        self._response_id, self._call_ids = response['id'], tuple(call_ids)
        return reply

    def cancel(self):
        self._cancel_event.set()
        return CancellationStatus.SERVER_UNKNOWN if self._attempted else CancellationStatus.LOCAL_CANCELLED

    def health_check(self):
        # Local readiness only; never a claim about remote service health.
        return self._transport is not None and not self._closed and not self._cancel_event.is_set()

    def close(self):
        self._closed = True
        self._cancel_event.set()
