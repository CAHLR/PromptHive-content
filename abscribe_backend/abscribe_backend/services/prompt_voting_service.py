import json
from functools import partial

convert_to_json = partial(
    json.dumps, indent=None, sort_keys=True, separators=(",", ":")
)

def 