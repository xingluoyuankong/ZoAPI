p = 'zo_client.py'
s = open(p).read()

s = s.replace(
    'account, name, prompt, scopes=[]',
    'account, name, prompt, scopes=["web:browse"]'
)

old_err = (
    '            if resp.status_code >= 400:\n'
    '                err = (await resp.aread()).decode("utf-8", errors="replace")\n'
    '                _raise_status(resp.status_code, err)'
)
new_err = (
    '            if resp.status_code >= 400:\n'
    '                err = (await resp.aread()).decode("utf-8", errors="replace")\n'
    '                log.warning("Zo /ask %d body: %s", resp.status_code, err[:1500])\n'
    '                _raise_status(resp.status_code, err)'
)
assert old_err in s, 'ask_stream block missing'
s = s.replace(old_err, new_err)

open(p, 'w').write(s)
print('patched')
import ast; ast.parse(open(p).read()); print('syntax OK')
