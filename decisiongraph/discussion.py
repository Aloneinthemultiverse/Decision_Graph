import uuid
import pickle
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import List
from . import config


@dataclass
class Message:
    role: str           # "user" or "agent"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str = ""
    messages: List[Message] = field(default_factory=list)
    summary: str = ""
    key_decisions: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    ended_at: str = ""
    duration_minutes: float = 0.0
    company_id: str = ""


class DiscussionManager:
    def __init__(self, storage_dir: str = None):
        self.storage_dir = storage_dir or os.path.join(config.STORAGE_DIR, "discussions")
        os.makedirs(self.storage_dir, exist_ok=True)
        self.active_session: Session = None
        self.sessions: dict = self._load_all_sessions()

    def _load_all_sessions(self) -> dict:
        path = os.path.join(self.storage_dir, "sessions.pkl")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return pickle.load(f)
        return {}

    def _save_all_sessions(self):
        path = os.path.join(self.storage_dir, "sessions.pkl")
        with open(path, "wb") as f:
            pickle.dump(self.sessions, f)

    def start_session(self, title: str = "", company_id: str = "") -> Session:
        self.active_session = Session(
            title=title or f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            company_id=company_id
        )
        print(f"Session started: {self.active_session.id} -- {self.active_session.title}")
        return self.active_session

    def add_message(self, role: str, content: str):
        if not self.active_session:
            raise ValueError("No active session. Call start_session() first.")
        msg = Message(role=role, content=content)
        self.active_session.messages.append(msg)

    def end_session(self, client, memory=None) -> Session:
        if not self.active_session:
            raise ValueError("No active session.")

        # calculate duration
        started = datetime.fromisoformat(self.active_session.started_at)
        ended = datetime.now()
        duration = (ended - started).total_seconds() / 60
        self.active_session.ended_at = ended.isoformat()
        self.active_session.duration_minutes = round(duration, 2)

        # generate summary
        print("Generating session summary...")
        self.active_session = self._generate_summary(client, self.active_session)

        # auto-store session summary as decision node in DecisionGraph
        if memory and self.active_session.summary:
            memory.store(
                question=f"Session: {self.active_session.title}",
                answer=self.active_session.summary,
                reasoning_summary=f"Key decisions: {' | '.join(self.active_session.key_decisions[:3])}",
                communities_used=[],
                context_triples=[
                    f"Session --[had_decision]--> {d}"
                    for d in self.active_session.key_decisions
                ] + [
                    f"Session --[has_open_question]--> {q}"
                    for q in self.active_session.open_questions
                ]
            )
            memory.save()
            print(f"Session stored in decision graph.")

        # save to sessions file
        self.sessions[self.active_session.id] = self.active_session
        self._save_all_sessions()

        print(f"Session ended: {self.active_session.id}")
        print(f"Duration: {duration:.1f} minutes")
        print(f"Summary: {self.active_session.summary[:100]}...")

        session = self.active_session
        self.active_session = None
        return session

    def _generate_summary(self, client, session: Session) -> Session:
        conversation = "\n".join([
            f"{m.role.upper()}: {m.content}"
            for m in session.messages
        ])

        if not conversation.strip():
            session.summary = "Empty session -- no messages."
            return session

        response = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": f"""Analyze this discussion and provide:
1. A 2-3 sentence summary of what was discussed
2. Key decisions made (as a list)
3. Open questions unresolved (as a list)

Format your response EXACTLY as:
SUMMARY: <summary here>
DECISIONS:
- <decision 1>
- <decision 2>
OPEN_QUESTIONS:
- <question 1>
- <question 2>

Discussion:
{conversation[:4000]}"""}]
        )

        raw = next(
            (b.text.strip() for b in response.content if b.type == "text" and b.text.strip()),
            ""
        )

        try:
            lines = raw.split("\n")
            summary = ""
            decisions = []
            open_questions = []
            current_section = None

            for line in lines:
                if line.startswith("SUMMARY:"):
                    summary = line.replace("SUMMARY:", "").strip()
                elif line.startswith("DECISIONS:"):
                    current_section = "decisions"
                elif line.startswith("OPEN_QUESTIONS:"):
                    current_section = "questions"
                elif line.startswith("- ") and current_section == "decisions":
                    decisions.append(line[2:].strip())
                elif line.startswith("- ") and current_section == "questions":
                    open_questions.append(line[2:].strip())

            session.summary = summary
            session.key_decisions = decisions
            session.open_questions = open_questions
        except:
            session.summary = raw[:300]

        return session

    def chat(self, question: str, client, dg_query_fn, memory=None, mode: str = "session") -> str:
        """Multi-turn chat within a session. dg_query_fn = DecisionGraph or CompanyMemory query fn."""
        if not self.active_session:
            self.start_session()

        self.add_message("user", question)
        answer = dg_query_fn(question, mode=mode)
        self.add_message("agent", answer)

        return answer

    def get_session_history(self) -> list:
        if not self.active_session:
            return []
        return [
            {"role": m.role, "content": m.content, "timestamp": m.timestamp}
            for m in self.active_session.messages
        ]

    def list_sessions(self, company_id: str = None) -> list:
        sessions = []
        for sid, session in self.sessions.items():
            if company_id and session.company_id != company_id:
                continue
            sessions.append({
                "id": session.id,
                "title": session.title,
                "summary": session.summary,
                "key_decisions": session.key_decisions,
                "open_questions": session.open_questions,
                "duration_minutes": session.duration_minutes,
                "started_at": session.started_at,
                "messages_count": len(session.messages),
                "company_id": session.company_id
            })
        return sorted(sessions, key=lambda x: x["started_at"], reverse=True)

    def get_session(self, session_id: str) -> dict:
        session = self.sessions.get(session_id)
        if not session:
            return None
        return {
            "id": session.id,
            "title": session.title,
            "summary": session.summary,
            "key_decisions": session.key_decisions,
            "open_questions": session.open_questions,
            "duration_minutes": session.duration_minutes,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "messages": [
                {"role": m.role, "content": m.content, "timestamp": m.timestamp}
                for m in session.messages
            ]
        }
