p='accounts.py'
s=open(p).read()

old = '    bridge_persona_id: str | None = None\n    bridge_persona_checked_at: float | None = None'
new = old + '\n    api_key: str | None = None\n    api_key_id: str | None = None'
assert old in s
s = s.replace(old, new, 1)

old2 = '"bridge_persona_id": self.bridge_persona_id,\n            "bridge_persona_checked_at": self.bridge_persona_checked_at,'
new2 = old2 + '\n            "api_key": self.api_key,\n            "api_key_id": self.api_key_id,'
assert old2 in s
s = s.replace(old2, new2, 1)

old3 = 'bridge_persona_id=d.get("bridge_persona_id"),\n            bridge_persona_checked_at=d.get("bridge_persona_checked_at"),'
new3 = old3 + '\n            api_key=d.get("api_key"),\n            api_key_id=d.get("api_key_id"),'
assert old3 in s
s = s.replace(old3, new3, 1)

open(p,'w').write(s)
import ast; ast.parse(open(p).read())
print('OK')
