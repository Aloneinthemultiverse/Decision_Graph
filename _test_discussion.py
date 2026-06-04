"""Test discussion module — multi-message session threads."""
import os
os.environ["LLM_BASE_URL"] = "http://localhost:8080"
os.environ["LLM_API_KEY"] = "test"
from dotenv import load_dotenv; load_dotenv()

from decisiongraph.discussion import DiscussionManager

dm = DiscussionManager(storage_dir=".bench/disc_ws")

# Start session
s = dm.start_session(title="auth refactor planning")
print(f"started: id={s.id}  title={s.title}")

# Add messages
dm.add_message("user", "should we keep JWT or migrate to OAuth2 with PKCE?")
dm.add_message("assistant", "Both have trade-offs. JWT is simpler but harder to revoke; OAuth2 supports proper revocation and refresh.")
dm.add_message("user", "we have <500 internal users, no third-party clients")
dm.add_message("assistant", "Then stick with JWT + short TTL + sliding refresh. Simpler, sufficient, less moving parts.")
print(f"added 4 messages")

# End the session so it persists
from decisiongraph.core import DecisionGraph
ws_dg = DecisionGraph(storage_dir=".bench/disc_ws")
ended = dm.end_session(ws_dg.client)
print(f"ended: summary_chars={len(ended.summary)}  duration_min={ended.duration_minutes:.2f}")

# Persist + reload
hist = dm.get_session_history()
print(f"history fetched: {len(hist)} messages")
for m in hist[:4]:
    role = m.get("role") if isinstance(m, dict) else m.role
    txt  = m.get("content") if isinstance(m, dict) else m.content
    print(f"  [{role}] {txt[:70]}")

# List sessions (across cold-restart)
dm2 = DiscussionManager(storage_dir=".bench/disc_ws")
ls = dm2.list_sessions()
print(f"\ncold-restart list_sessions: {len(ls)} sessions")
for sess in ls[:3]:
    sid = sess.get("session_id", sess.get("id"))
    print(f"  - {sid}  title={sess.get('title')}")

# get_session by id
g = dm2.get_session(s.id)
print(f"\nget_session by id: keys={list(g.keys()) if isinstance(g, dict) else 'object'}")
msgs = g.get("messages") if isinstance(g, dict) else g.messages
print(f"  messages preserved: {len(msgs)}")
