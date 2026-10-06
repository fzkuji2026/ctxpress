"""Stands in for Codex in tests: reads -c openai_base_url=..., sends one Responses request with the history in
FAKE_INPUT (a JSON file) and exits."""
import json, os, sys, urllib.request
try:
    import tomllib
except ModuleNotFoundError:                     # Python 3.10
    from ctxpress._vendor import tomllib
args = sys.argv[1:]
assert args[0] == "--profile" and args[1].startswith("ctxpress"), args
prof = os.path.join(os.environ["CODEX_HOME"], args[1] + ".config.toml")
base = tomllib.load(open(prof, "rb"))["openai_base_url"]
inp = json.load(open(os.environ["FAKE_INPUT"], encoding="utf-8"))
req = urllib.request.Request(base + "/responses", data=json.dumps({"model": "m", "input": inp, "prompt_cache_key": "k"}).encode(),
                             headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
print(opener.open(req, timeout=30).read().decode()[:200])
