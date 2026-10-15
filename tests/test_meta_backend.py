import unittest


class MetaBackendTests(unittest.TestCase):
    def test_text_wire(self):
        from everquill.meta_backend import MetaModelBackend
        seen = []
        def transport(**kwargs):
            seen.append(kwargs)
            return {'id': 'resp_opaque', 'status': 'completed', 'output': [
                {'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': 'answer'}]}]}
        backend = MetaModelBackend(transport=transport)
        backend.send_message([{'role': 'user', 'content': 'question'}], metadata={'session_id': 'opaque'})
        self.assertEqual(backend.stream_response().text, 'answer')
        self.assertEqual(seen[0]['url'], 'https://api.meta.ai/v1/responses')
        self.assertEqual(seen[0]['payload'], {'model': 'muse-spark-1.3', 'input': [
            {'role': 'user', 'content': 'question'}], 'stream': True, 'previous_response_id': 'opaque'})
        self.assertEqual(seen[0]['headers'], {'Content-Type': 'application/json'})


class MetaMappingTests(unittest.TestCase):
    def backend(self, response, **kwargs):
        from everquill.meta_backend import MetaModelBackend
        backend = MetaModelBackend(transport=lambda **unused: response, **kwargs)
        backend.send_message([{'role': 'user', 'content': 'hello'}])
        return backend

    def response(self, output=None):
        return {'id': 'resp_private', 'status': 'completed', 'output': output if output is not None else [
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ok'}]}]}

    def frame(self, event):
        import json
        return 'data: ' + json.dumps(event) + '\n\n'

    def test_tool_mapping_no_dispatch(self):
        call = {'type': 'function_call', 'id': 'fc1', 'call_id': 'call1', 'name': 'read_file', 'arguments': '{"path":"x"}'}
        backend = self.backend(self.response([call]))
        reply = backend.stream_response()
        self.assertEqual(reply.status, 'tool_request')
        self.assertEqual(reply.tool_calls[0].name, 'read_file')
        self.assertEqual(dict(reply.tool_calls[0].arguments), {'path': 'x'})

    def test_tool_schema_wire(self):
        from everquill.meta_backend import MetaModelBackend
        seen = []
        backend = MetaModelBackend(transport=lambda **kw: seen.append(kw) or self.response(), api_key='offline-placeholder')
        tool = {'type': 'function', 'name': 'read_file', 'description': 'Read', 'parameters': {'type': 'object', 'properties': {}}}
        backend.send_message([{'role': 'user', 'content': 'hello'}], tools=[tool])
        backend.stream_response()
        self.assertEqual(seen[0]['payload']['tools'], [tool])
        self.assertEqual(seen[0]['headers']['Authorization'], 'Bearer offline-placeholder')

    def test_sse_final_only(self):
        events = [self.frame({'type': 'response.created', 'response': {'id': 'resp_private'}}),
                  self.frame({'type': 'response.output_text.delta', 'delta': 'untrusted'}),
                  self.frame({'type': 'response.completed', 'response': self.response()})]
        self.assertEqual(self.backend(events).stream_response().text, 'ok')

    def test_sse_tool_final(self):
        call = {'type': 'function_call', 'call_id': 'c', 'name': 'read', 'arguments': '{}'}
        events = [self.frame({'type': 'response.function_call_arguments.delta', 'delta': '{'}),
                  self.frame({'type': 'response.completed', 'response': self.response([call])}).encode()]
        self.assertEqual(self.backend(events).stream_response().status, 'tool_request')

    def test_fail_closed_outputs(self):
        bad = [None, {}, self.response([]), self.response([{'type': 'unknown'}]),
               self.response([{'type': 'function_call', 'name': 'read', 'call_id': 'c', 'arguments': '[]'}]),
               self.response([{'type': 'function_call', 'name': 'read', 'arguments': '{}'}]),
               self.response([{'type': 'message', 'role': 'assistant', 'content': []}]),
               {**self.response(), 'status': 'incomplete'}, {**self.response(), 'id': ''},
               {**self.response(), 'output': 'oops'}]
        for response in bad:
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, '^backend response or transport failed$'):
                self.backend(response).stream_response()

    def test_mixed_text_tools_preserved(self):
        output = self.response()['output'] + [{'type': 'function_call', 'call_id': 'c', 'name': 'read', 'arguments': '{}'}]
        reply = self.backend(self.response(output)).stream_response()
        self.assertEqual(reply.text, 'ok')
        self.assertEqual(reply.tool_calls[0].call_id, 'c')
        self.assertEqual(reply.response_id, 'resp_private')

    def test_sse_malformed_partial_empty_failed(self):
        valid = self.frame({'type': 'response.completed', 'response': self.response()})
        bad = [[], ['data: invalid\n\n'], ['data: {}\n\n'],
               [valid.rstrip()], [self.frame({'type': 'response.output_text.delta', 'delta': 'partial'})],
               [self.frame({'type': 'response.failed', 'response': self.response()})],
               [valid, valid], [valid, 'data: invalid\n\n'],
               [self.frame({'type': 'response.created', 'response': {'id': 'other'}}), valid]]
        for events in bad:
            with self.subTest(events=events), self.assertRaises(RuntimeError):
                self.backend(events).stream_response()

    def test_timeout_local(self):
        from everquill.meta_backend import MetaModelBackend
        def fail(**kw):
            self.assertEqual(kw['timeout'], 1)
            raise TimeoutError('secret')
        backend = MetaModelBackend(transport=fail, timeout=1)
        backend.send_message([{'role': 'user', 'content': 'hello'}])
        self.assertEqual(backend.stream_response().status, 'timeout')
        self.assertEqual(backend.cancel().value, 'SERVER_UNKNOWN')

    def test_cancel_before_transport(self):
        backend = self.backend(self.response())
        self.assertEqual(backend.cancel().value, 'LOCAL_CANCELLED')
        self.assertEqual(backend.stream_response().status, 'cancelled')

    def test_cancel_during_stream(self):
        backend = self.backend(None)
        def events():
            backend.cancel()
            yield self.frame({'type': 'response.completed', 'response': self.response()})
        backend._transport = lambda **kw: events()
        self.assertEqual(backend.stream_response().status, 'cancelled')

    def test_sanitized_error(self):
        backend = self.backend(None)
        def fail(**kw):
            raise ValueError('secret token request body')
        backend._transport = fail
        with self.assertRaisesRegex(RuntimeError, '^backend response or transport failed$') as caught:
            backend.stream_response()
        self.assertTrue(caught.exception.__suppress_context__)

    def test_no_default_network_missing_key(self):
        from everquill.meta_backend import MetaModelBackend
        backend = MetaModelBackend()
        backend.send_message([{'role': 'user', 'content': 'hello'}])
        with self.assertRaisesRegex(RuntimeError, 'authentication required'):
            backend.stream_response()
        backend = MetaModelBackend(api_key='offline-placeholder')
        backend.send_message([{'role': 'user', 'content': 'hello'}])
        with self.assertRaisesRegex(RuntimeError, 'live transport is disabled'):
            backend.stream_response()

    def test_input_validation(self):
        from everquill.meta_backend import MetaModelBackend
        for messages in [[], {}, [{'role': 'tool', 'content': 'x'}], [{'role': 'user', 'content': ''}]]:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                MetaModelBackend().send_message(messages)
        for metadata in [{'unexpected': 'x'}, {'session_id': ''}, {'session_id': 1}, []]:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                MetaModelBackend().send_message([{'role': 'user', 'content': 'x'}], metadata=metadata)

    def test_stable_final_does_not_resend(self):
        backend = self.backend(self.response())
        first = backend.stream_response()
        def fail(**kw):
            raise AssertionError('must not resend')
        backend._transport = fail
        self.assertEqual(backend.stream_response(), first)

    def test_protocol_signatures_unchanged(self):
        import inspect
        from everquill.meta_backend import MetaModelBackend
        from everquill.reasoning import ReasoningBackend
        for name in ['start_session', 'send_message', 'stream_response', 'cancel', 'health_check', 'close']:
            self.assertEqual(list(inspect.signature(getattr(MetaModelBackend, name)).parameters),
                             list(inspect.signature(getattr(ReasoningBackend, name)).parameters))

    def test_private_correlation(self):
        call = {'type': 'function_call', 'call_id': 'c', 'name': 'read', 'arguments': '{}'}
        backend = self.backend(self.response([call]))
        backend.stream_response()
        self.assertEqual(backend._response_id, 'resp_private')
        self.assertEqual(backend._call_ids, ('c',))

    def test_malformed_json_arguments(self):
        for arguments in ['{', '{"x":1,"x":2}', '{"x":NaN}', 'null', '{} trailing']:
            call = {'type': 'function_call', 'call_id': 'c', 'name': 'read', 'arguments': arguments}
            with self.subTest(arguments=arguments), self.assertRaises(RuntimeError):
                self.backend(self.response([call])).stream_response()

    def test_sse_structural_errors(self):
        final = self.frame({'type': 'response.completed', 'response': self.response()})
        for event in [{'type': 'response.created'}, {'type': 'response.output_item.done'},
                      {'type': 'response.output_text.delta', 'delta': 7}]:
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                self.backend([self.frame(event), final]).stream_response()

    def test_configuration_rejects_invalid_values(self):
        from everquill.meta_backend import MetaModelBackend
        for timeout in [0, -1, True, '1', float('nan'), float('inf')]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                MetaModelBackend(timeout=timeout)
        for key in ['', 1, 'x\nHeader: y']:
            with self.subTest(key=key), self.assertRaises(ValueError):
                MetaModelBackend(api_key=key)

    def test_multi_turn_uses_previous_response_id(self):
        from everquill.meta_backend import MetaModelBackend
        seen = []
        backend = MetaModelBackend(transport=lambda **kw: seen.append(kw) or self.response())
        backend.send_message([{'role': 'user', 'content': 'first'}])
        backend.stream_response()
        backend.send_message([{'role': 'user', 'content': 'second'}])
        backend.stream_response()
        self.assertEqual(seen[1]['payload']['previous_response_id'], 'resp_private')

    def test_provider_status_mapping_is_sanitized(self):
        from everquill.meta_backend import ProviderStatusError
        for status, category in [(401, 'authentication'), (403, 'authorization'),
                                 (429, 'rate_limit'), (500, 'provider'), (400, 'request')]:
            backend = self.backend(None)
            def fail(**kw):
                error = RuntimeError('private provider payload')
                error.status_code = status
                raise error
            backend._transport = fail
            with self.assertRaises(ProviderStatusError) as caught:
                backend.stream_response()
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(caught.exception.category, category)
            self.assertNotIn('private', str(caught.exception))
            self.assertIsNone(caught.exception.__context__)

    def test_sse_done_marker_after_terminal(self):
        frames = [self.frame({'type': 'response.completed', 'response': self.response()}),
                  'data: [DONE]\n\n']
        self.assertEqual(self.backend(frames).stream_response().text, 'ok')
        with self.assertRaises(RuntimeError):
            self.backend(['data: [DONE]\n\n']).stream_response()

    def test_lifecycle_and_bounded_sessions(self):
        backend = self.backend(self.response())
        self.assertTrue(backend.health_check())
        with self.assertRaises(NotImplementedError):
            backend.start_session('project')
        backend.close()
        self.assertFalse(backend.health_check())
        with self.assertRaises(RuntimeError):
            backend.stream_response()


if __name__ == '__main__':
    unittest.main()
