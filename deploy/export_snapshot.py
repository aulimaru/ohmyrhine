"""Export a private debugging snapshot with aliases for known participants.

Runs on the gateway host. Each person becomes a stable alias (群友01, 群友02 ...) and each chat
G1, G2 ...; known nicknames are replaced in structured names and in free text (only names of
two or more characters there). Media files are not
exported. Free text, connector references and journal metadata can still identify people or
machines. Keep the output with private runtime data. The alias table is never written out.

usage: uv run python deploy/export_snapshot.py OUT_DIR
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from ohmyrhine.engine.prompt import display_id
from ohmyrhine.store import INSTRUCTION_SENDER, LOOKUP_SENDER, SYSTEM_SENDER

DATA = Path("data")
SYSTEM_SENDERS = (SYSTEM_SENDER, INSTRUCTION_SENDER, LOOKUP_SENDER)


def people(dbs: list[Path]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """(user id -> alias id, nickname -> alias, chat id -> alias), consistent across databases."""
    seen: dict[str, int] = {}
    names: dict[str, str] = {}
    chats: dict[str, str] = {}
    for db in dbs:
        con = sqlite3.connect(db)
        for uid, nick, nicks, first in con.execute(
            "SELECT id, nickname, nicknames, first_seen FROM users WHERE id NOT IN (?, ?, ?)", SYSTEM_SENDERS
        ):
            seen[uid] = min(seen.get(uid, first), first)
            for n in {nick, *json.loads(nicks or "[]")}:
                names.setdefault(n, uid)
        rows = con.execute("SELECT DISTINCT sender_id, sender_name FROM messages WHERE role = 'user' AND sender_id NOT IN (?, ?, ?)",
                           SYSTEM_SENDERS)
        for uid, name in rows:
            seen.setdefault(uid, 1 << 62)
            names.setdefault(name, uid)
        for (cid,) in con.execute("SELECT id FROM chats ORDER BY created_at"):
            chats.setdefault(cid, f"im:g:G{len(chats) + 1}")
        con.close()
    order = sorted(seen, key=lambda u: seen[u])
    ids = {u: f"im:u:A{i + 1:02d}" for i, u in enumerate(order)}
    alias = {u: f"群友{i + 1:02d}" for i, u in enumerate(order)}
    nick = {n: alias[u] for n, u in names.items() if n and u in alias}
    return ids, nick, chats


class Scrubber:
    def __init__(self, ids: dict[str, str], nick: dict[str, str], chats: dict[str, str]):
        self.exact = dict(nick)
        # Free text: names of two or more characters, longest first. One-character names would
        # mangle ordinary words, so they are only replaced where a field holds just the name.
        long = sorted((n for n in nick if len(n) >= 2), key=len, reverse=True)
        self.names = re.compile("|".join(map(re.escape, long))) if long else None
        self.ids = {**ids, **{display_id(u): display_id(a) for u, a in ids.items()}, **chats}
        self.ids |= {c[:5] + c[-6:]: a for c, a in chats.items() if len(c) > 16}  # the gateway log's short form
        self.id_re = re.compile("|".join(map(re.escape, sorted(self.ids, key=len, reverse=True)))) if self.ids else None

    def name(self, s: str | None) -> str | None:
        return self.exact.get(s, self.text(s)) if s else s

    def text(self, s: str | None) -> str | None:
        if not s:
            return s
        if self.id_re:
            s = self.id_re.sub(lambda m: self.ids[m.group(0)], s)
        return self.names.sub(lambda m: self.exact[m.group(0)], s) if self.names else s

    def id(self, s: str | None) -> str | None:
        return self.ids.get(s, s) if s else s


def copy_db(src: Path, dst: Path) -> None:
    with closing(sqlite3.connect(src)) as a, closing(sqlite3.connect(dst)) as b:
        a.backup(b)


def scrub_db(path: Path, sc: Scrubber) -> None:
    con = sqlite3.connect(path)
    with con:
        rows = con.execute("SELECT id, sender_id, sender_name, content, quote, digest FROM messages").fetchall()
        for mid, sid, sname, content, quote, digest in rows:
            if quote:
                q = json.loads(quote)
                q["sender_name"], q["text"] = sc.name(q.get("sender_name")), sc.text(q.get("text"))
                quote = json.dumps(q, ensure_ascii=False)
            con.execute(
                "UPDATE messages SET sender_id = ?, sender_name = ?, content = ?, quote = ?, digest = ? WHERE id = ?",
                (sc.id(sid), sc.name(sname), sc.text(content), quote, sc.text(digest), mid),
            )
        for (cid,) in con.execute("SELECT DISTINCT chat_id FROM messages").fetchall():
            con.execute("UPDATE messages SET chat_id = ? WHERE chat_id = ?", (sc.id(cid), cid))
        for mid, cid, subj, text in con.execute("SELECT id, chat_id, subject_id, text FROM memories").fetchall():
            con.execute("UPDATE memories SET chat_id = ?, subject_id = ?, text = ? WHERE id = ?",
                        (sc.id(cid), sc.id(subj), sc.text(text), mid))
        if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_changes'").fetchone():
            for cid, text, before, reason in con.execute("SELECT id, text, before, reason FROM memory_changes").fetchall():
                con.execute("UPDATE memory_changes SET text = ?, before = ?, reason = ? WHERE id = ?",
                            (sc.text(text), sc.text(before), sc.text(reason), cid))
        for cid, summary in con.execute("SELECT id, summary FROM chats").fetchall():
            con.execute("UPDATE chats SET id = ?, summary = ? WHERE id = ?", (sc.id(cid), sc.text(summary), cid))
        for sid, cid, text in con.execute("SELECT id, chat_id, text FROM scenes").fetchall():
            con.execute("UPDATE scenes SET chat_id = ?, text = ? WHERE id = ?", (sc.id(cid), sc.text(text), sid))
        for sha, desc in con.execute("SELECT sha256, description FROM media").fetchall():
            con.execute("UPDATE media SET description = ? WHERE sha256 = ?", (sc.text(desc), sha))
        for aid, hint in con.execute("SELECT id, hint FROM attachments WHERE hint IS NOT NULL").fetchall():
            con.execute("UPDATE attachments SET hint = ? WHERE id = ?", (sc.name(hint), aid))
        users = con.execute("SELECT id, nickname, nicknames FROM users").fetchall()
        for uid, nick, nicks in users:
            new = sorted({sc.name(n) or "" for n in json.loads(nicks or "[]")} | {sc.name(nick) or ""})
            con.execute("UPDATE users SET id = ?, nickname = ?, nicknames = ? WHERE id = ?",
                        (sc.id(uid), sc.name(nick), json.dumps(new, ensure_ascii=False), uid))
    con.execute("VACUUM")
    con.close()


def export_jsonl(src: Path, dst: Path, sc: Scrubber) -> int:
    n = 0
    with src.open(encoding="utf-8") as f, dst.open("w", encoding="utf-8") as out:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:  # a line cut short by a crash or a full disk: left out, never copied raw
                continue
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                if isinstance(v, str):
                    r[k] = sc.id(v) if k == "chat_id" else sc.text(v)
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as tmp:
        snapshots = []
        for db in sorted(DATA.glob("*.db")):
            snapshot = Path(tmp) / db.name
            copy_db(db, snapshot)
            snapshots.append(snapshot)
        ids, nick, chats = people(snapshots)
        sc = Scrubber(ids, nick, chats)
        for db in snapshots:
            scrub_db(db, sc)
            copy_db(db, out / db.name)
    (out / "logs").mkdir()
    for log in sorted((DATA / "logs").glob("*.jsonl")):
        print(f"{log.name}: {export_jsonl(log, out / 'logs' / log.name, sc)} lines")
    journal = subprocess.run(
        ["journalctl", "--user", "-u", "ohmyrhine-gateway", "--no-pager", "--no-hostname", "-o", "short-iso"],
        capture_output=True, text=True, check=True,
    ).stdout
    (out / "logs" / "gateway.log").write_text("\n".join(sc.text(l) or "" for l in journal.splitlines()) + "\n")
    print(f"{len(ids)} people, {len(nick)} nicknames ({sum(len(n) < 2 for n in nick)} one-character), {len(chats)} chats")
    # A check that no nickname of two or more characters survived anywhere in the export.
    left = 0
    if sc.names:
        for f in out.rglob("*"):
            if f.is_file() and f.suffix in (".log", ".jsonl"):
                left += len(sc.names.findall(f.read_text(encoding="utf-8")))
            elif f.suffix == ".db":
                con = sqlite3.connect(f)
                changes = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_changes'").fetchone()
                for sql in ("SELECT content FROM messages", "SELECT quote FROM messages", "SELECT text FROM memories",
                            *(("SELECT text || ifnull(before, '') || ifnull(reason, '') FROM memory_changes",) if changes else ()),
                            "SELECT summary FROM chats", "SELECT description FROM media", "SELECT nicknames FROM users",
                            "SELECT hint FROM attachments"):
                    left += sum(len(sc.names.findall(v)) for (v,) in con.execute(sql) if v)
                con.close()
    print("nicknames left:", left)


if __name__ == "__main__":
    main()
