"""Compare Ollama models on LuckyLook questions whose answers are checked against this PC.

Run:  python bench.py qwen3.5:9b qwen3:8b ...      (add --think to turn on the models' thinking mode)
Needs the index to cover this folder (run the popup or indexer.py first). Add questions about your own files to
TESTS for a sharper comparison.
"""
import json
import shutil
import sys
import time
import urllib.request

import agent

TESTS = [  # (question, words that must all appear in the answer)
    ("Where is the file test_lucky.py on this computer?", [str(agent.HERE)]),
    ("According to the LuckyLook README, which hotkey opens the popup?", ["Ctrl+Alt+L"]),
    ("How much free space is on my C drive?", [agent.human(shutil.disk_usage("C:\\").free).split()[0]]),
    ("What is the biggest file in the folder " + str(agent.HERE / "assets") + "?", ["logo.png"]),
    ("What is 17 times 23?", ["391"]),
]


def api(path, body):
    req = urllib.request.Request(f"http://localhost:11434/api/{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read() or "{}")


def speed(model):
    """Generation tokens/sec and share of the model that sits in GPU memory."""
    r = api("generate", {"model": model, "prompt": "Write 150 words about rivers.", "stream": False, "think": False,
                         "options": {"num_ctx": agent.NUM_CTX}})
    with urllib.request.urlopen("http://localhost:11434/api/ps") as p:
        m = next(m for m in json.load(p)["models"] if m["name"] == model)
    return r["eval_count"] / r["eval_duration"] * 1e9, m["size_vram"] / m["size"]


def bench(model):
    agent.MODEL = model
    with urllib.request.urlopen("http://localhost:11434/api/ps") as p:  # free VRAM held by other models
        for m in json.load(p)["models"]:
            api("generate", {"model": m["name"], "keep_alive": 0})
    t = time.time()
    tps, gpu = speed(model)
    load = time.time() - t
    passed, total_s = 0, 0.0
    for q, must in TESTS:
        tools = []
        t = time.time()
        try:
            ans = agent.ask(q, on_text=lambda s: None, on_tool=lambda n, a: tools.append(n))
        except Exception as e:
            ans = f"ERROR {e}"
        dt = time.time() - t
        total_s += dt
        norm = ans.lower()
        ok = all(w.lower() in norm or w.lower().replace(",", "") in norm for w in must)
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'} {dt:5.1f}s {','.join(tools) or '-':40} {q}")
        if not ok:
            print("       ->", ans.strip().replace("\n", " ")[:200])
    print(f"{model}: {passed}/{len(TESTS)} correct · {total_s / len(TESTS):.1f}s avg · {tps:.0f} tok/s · "
          f"{gpu:.0%} on GPU · first load+warmup {load:.0f}s\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    agent.THINK = "--think" in sys.argv
    for m in (a for a in sys.argv[1:] if a != "--think"):
        print(m, "(thinking)" if agent.THINK else "")
        bench(m)
