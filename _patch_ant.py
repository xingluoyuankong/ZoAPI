p = 'anthropic_sse.py'
s = open(p).read()

old = """            elif kind == 'tool_args':
                yield from self._delta_tool_input(payload)
            elif kind == 'tool_close':
                pass"""

new = """            elif kind == 'tool_args':
                if self._zo_text_tool_rename:
                    self._zo_text_tool_arg_buf.append(payload)
                else:
                    yield from self._delta_tool_input(payload)
            elif kind == 'tool_close':
                if self._zo_text_tool_rename:
                    import json as _json
                    raw = ''.join(self._zo_text_tool_arg_buf)
                    try:
                        args = _json.loads(raw or '{}')
                        from tool_bridge import remap_args
                        args = remap_args('', args, self._zo_text_tool_rename)
                        yield from self._delta_tool_input(_json.dumps(args, ensure_ascii=False))
                    except Exception:
                        yield from self._delta_tool_input(raw)
                    self._zo_text_tool_arg_buf = []
                    self._zo_text_tool_rename = {}
                el