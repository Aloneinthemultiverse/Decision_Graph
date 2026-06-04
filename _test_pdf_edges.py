"""PDF edge cases."""
import os
from decisiongraph.codebase_ast import extract_text_from_any

# 1. Missing file
print("[1] missing file:")
r = extract_text_from_any(".bench/does_not_exist.pdf")
print(f"   {r}")

# 2. Empty/bogus PDF (just a few bytes)
print("\n[2] bogus PDF bytes:")
open(".bench/bogus.pdf", "wb").write(b"not a real pdf")
r = extract_text_from_any(".bench/bogus.pdf")
print(f"   kind={r.get('kind')}  text_len={len(r.get('text', ''))}  "
      f"error={r.get('error', '')[:80]}")

# 3. Truncated PDF (real PDF header + garbage)
print("\n[3] truncated PDF:")
content = open(".bench/test.pdf", "rb").read()
open(".bench/truncated.pdf", "wb").write(content[:2000])  # cut to 2 KB
r = extract_text_from_any(".bench/truncated.pdf")
print(f"   kind={r.get('kind')}  text_len={len(r.get('text', ''))}  "
      f"error={r.get('error', '')[:80]}")

# 4. Large PDF — check the 40-page cap
print("\n[4] real PDF (25 pages, no cap hit):")
r = extract_text_from_any(".bench/test.pdf")
print(f"   page_count={r.get('page_count')}  text_len={len(r.get('text', ''))}")

# 5. Cleanup
for f in (".bench/bogus.pdf", ".bench/truncated.pdf"):
    if os.path.exists(f): os.remove(f)
print("\nall handled gracefully (no crashes)")
