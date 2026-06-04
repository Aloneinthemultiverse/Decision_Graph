"""Step 3: Apply the agent's patch + commit; hook will rebuild graph."""
import re, subprocess
from pathlib import Path

REPO_PATH = Path(r"C:\Users\SUJITN~1\AppData\Local\Temp\loop_flask")
TARGET    = REPO_PATH / "src/flask/ctx.py"
NEW_CLASS = Path(".bench/loop_agent_class.py").read_text(encoding="utf-8")

# Original source
src = TARGET.read_text(encoding="utf-8")

# Splice in: replace the existing AppContext class block.
# Strategy: find `class AppContext:` and the next top-level boundary.
pattern = re.compile(
    r"class AppContext:.*?(?=\nclass |\Z)", re.DOTALL)
m = pattern.search(src)
if not m:
    raise SystemExit("FAIL: AppContext class not found in original file")
print(f"existing AppContext block: lines {src[:m.start()].count(chr(10))+1} "
      f"to {src[:m.end()].count(chr(10))+1}")

new_src = src[:m.start()] + NEW_CLASS.rstrip() + "\n\n" + src[m.end():]
TARGET.write_text(new_src, encoding="utf-8")
print(f"wrote new ctx.py ({len(new_src)} chars, was {len(src)})")

# git add + commit; this should fire the post-commit hook
out = subprocess.run(["git", "add", "src/flask/ctx.py"],
                      cwd=REPO_PATH, capture_output=True, text=True)
print("git add:", out.returncode)
out = subprocess.run(
    ["git", "commit", "-m", "feat: add AppContext.mark_dirty()"],
    cwd=REPO_PATH, capture_output=True, text=True)
print(f"git commit: exit={out.returncode}")
print(f"  stdout: {out.stdout.strip()}")
print(f"  stderr: {(out.stderr or '').strip()[:300]}")
