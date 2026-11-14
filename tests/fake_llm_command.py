"""Stands in for a chat plan's command-line tool (like `claude -p`): prompt on stdin, answer on stdout.
Usage in tests:  LLM_COMMAND="python tests/fake_llm_command.py http://127.0.0.1:<FakeLLM port>"
"""
import json
import sys
import urllib.request

prompt = sys.stdin.read()
req = urllib.request.Request(sys.argv[1] + "/api/chat", headers={"content-type": "application/json"},
                             data=json.dumps({"via": "command", "messages": [{"role": "user", "content": prompt}]}).encode())
print(json.loads(urllib.request.urlopen(req, timeout=60).read())["message"]["content"])
