"""Live Book Editor: conversational commands applied to the saved book.

A deterministic parser handles the documented command forms. If it cannot
understand a message and a text model is configured, the model translates the
message into the same structured command schema, which is then validated and
executed by the same code path (the model never touches data directly).

Safeguards: locked pages are never modified; approved pages need explicit
confirmation; expensive operations need confirmation; image work is queued so
it runs after any generation already in progress; every change is logged in
the edit history and can be undone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import battle, captions, costs, generation, jobs, routing, services
from .models import ChatMessage, Page, Project, Section
from .providers import registry
from .text import llm

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20}
PROVIDER_ALIASES = {
    "openai": "openai", "gpt": "openai", "gpt-image": "openai", "dall-e": "openai",
    "google": "google", "gemini": "google", "imagen": "google",
    "flux": "bfl", "bfl": "bfl", "black forest": "bfl",
    "stability": "stability", "stable diffusion": "stability", "stable image": "stability",
    "replicate": "replicate", "fal": "fal", "fal.ai": "fal",
    "mock": "mock", "mock studio": "mock", "atelier": "mock_alt", "mock_alt": "mock_alt", "mock alt": "mock_alt",
}
CONFIRM_COST_USD = 2.0


@dataclass
class Command:
    intent: str
    pages: list[int] = field(default_factory=list)
    sections: list[int] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    text: str = ""
    count: int = 0
    steps: int = 0
    all_pages: bool = False
    raw: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ([], "", 0, False)}


# ------------------------------------------------------------------ parsing

def _num(tok: str) -> int | None:
    tok = tok.strip().lower()
    if tok.isdigit():
        return int(tok)
    return NUM_WORDS.get(tok)


def parse_pages(text: str) -> list[int]:
    out: list[int] = []
    for m in re.finditer(r"pages?\s+((?:\d+\s*(?:-|–|to|through|and|,|&)?\s*)+)", text, re.I):
        chunk = m.group(1)
        for a, b in re.findall(r"(\d+)\s*(?:-|–|to|through)\s*(\d+)", chunk):
            out.extend(range(int(a), int(b) + 1))
        chunk = re.sub(r"(\d+)\s*(?:-|–|to|through)\s*(\d+)", "", chunk)
        out.extend(int(x) for x in re.findall(r"\d+", chunk))
    return sorted(dict.fromkeys(out))


def parse_sections(text: str) -> list[int]:
    out = [int(x) for x in re.findall(r"sections?\s+(\d+)", text, re.I)]
    return sorted(dict.fromkeys(out))


def parse_providers(text: str) -> list[str]:
    """Provider ids in order of first mention (longest alias wins at each position)."""
    low = text.lower()
    hits: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for alias in sorted(PROVIDER_ALIASES, key=len, reverse=True):
        for m in re.finditer(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", low):
            if any(a <= m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), PROVIDER_ALIASES[alias]))
    return list(dict.fromkeys(pid for _, pid in sorted(hits)))


def split_commands(message: str) -> list[str]:
    parts = re.split(r"\n+|;\s*|\.\s+(?=[A-Z])|\s+and then\s+|\s+then\s+", message.strip())
    return [p.strip(" .") for p in parts if p.strip(" .")]


def parse(message: str) -> list[Command]:
    cmds = []
    for part in split_commands(message):
        c = parse_one(part)
        if c:
            cmds.append(c)
    return cmds


def parse_one(t: str) -> Command | None:
    low = t.lower().strip()
    pages, secs, provs = parse_pages(t), parse_sections(t), parse_providers(t)
    all_pages = bool(re.search(r"\ball (?:the )?(?:pages|illustrations)\b|\bentire book\b|\bwhole book\b", low))
    if re.fullmatch(r"(yes|confirm|confirmed|go ahead|do it|proceed)[.!]?", low):
        return Command("confirm", raw=t)
    if re.fullmatch(r"(no|cancel|never ?mind|stop)[.!]?", low):
        return Command("decline", raw=t)
    if re.search(r"\b(help|what can you do)\b", low):
        return Command("help", raw=t)
    if re.search(r"\bundo\b", low):
        return Command("undo", pages=pages, raw=t)
    m = re.search(r"\brestore page (\d+)(?: to (?:revision )?([0-9a-f]{6,32}|previous|original))?", low)
    if m:
        return Command("restore", pages=[int(m.group(1))], text=m.group(2) or "previous", raw=t)
    if re.search(r"keep (?:all )?(?:the )?approved (?:pages|illustrations)? ?(?:unchanged|locked|as they are)|"
                 r"lock (?:all )?(?:the )?approved", low):
        return Command("lock_approved", raw=t)
    if re.search(r"\bunlock\b", low):
        return Command("unlock", pages=pages, sections=secs, all_pages=all_pages, raw=t)
    if re.search(r"\block\b", low):
        return Command("lock", pages=pages, sections=secs, all_pages=all_pages, raw=t)
    if re.search(r"\bapprove\b", low) and "approved" not in low:
        return Command("approve", pages=pages, sections=secs, all_pages=all_pages, raw=t)
    if re.search(r"\breject\b|request (?:a )?revision|needs? (?:a )?revision", low):
        return Command("reject", pages=pages, sections=secs, text=t, raw=t)
    if re.search(r"\b(compare|battle|side by side)\b", low) or re.search(r"\btry (?:the same|page|it).* in ", low):
        return Command("compare", pages=pages, providers=provs, raw=t)
    m = re.search(r"(?:set|change|make) (?:the )?caption (?:on|for|of) page (\d+) (?:to|say|read)[: ]+[\"“']?(.+?)[\"”']?$",
                  t, re.I)
    if m:
        return Command("set_caption", pages=[int(m.group(1))], text=m.group(2).strip(), raw=t)
    if "caption" in low:
        style = ""
        for word in ("shorter", "simpler", "longer", "funnier", "question", "vocabulary"):
            if word in low:
                style = word
        return Command("rewrite_caption", pages=pages, sections=secs, all_pages=all_pages, text=style, raw=t)
    m = re.search(r"(thicker|bolder|heavier|thinner|lighter|finer)\s+(?:lines|outlines)|"
                  r"(?:lines|outlines)\s+(?:a (?:bit|little) )?(thicker|bolder|heavier|thinner|lighter|finer)", low)
    if m:
        word = m.group(1) or m.group(2)
        steps = 1 if word in ("thicker", "bolder", "heavier") else -1
        if re.search(r"\bmuch\b|\bvery\b|\ba lot\b", low):
            steps *= 2
        return Command("line_weight", pages=pages, sections=secs, all_pages=all_pages, steps=steps, raw=t)
    m = re.search(r"change page (\d+) to (?:show|be|depict|feature|draw)\s+(.+?)(?:\s+instead of\s+(.+))?$", t, re.I)
    if m:
        return Command("change_subject", pages=[int(m.group(1))], text=m.group(2).strip(), providers=provs, raw=t)
    m = re.search(r"make section (\d+) (more|less) (.+)$", t, re.I)
    if m:
        return Command("section_instructions", sections=[int(m.group(1))],
                       text=f"{m.group(2).lower()} {m.group(3).strip()}", raw=t)
    m = re.search(r"use (?:the )?(.+?) (?:provider |model )?for (?:the )?(next section|section (\d+)|pages?)", low)
    if m and ("provider" in low or "model" in low or provs or "highest" in low or "best" in low or "cheapest" in low):
        target = []
        if m.group(3):
            target = [int(m.group(3))]
        rank = "highest_quality" if re.search(r"highest|best|top", low) else (
            "lowest_cost" if "cheapest" in low or "lowest" in low else "")
        return Command("set_provider", sections=target, providers=provs, pages=pages, text=rank or m.group(2), raw=t)
    m = re.search(r"generate (?:the )?next (\w+)", low)
    if m:
        return Command("generate_next", count=_num(m.group(1)) or 5, providers=provs, raw=t)
    if re.search(r"\b(re-?generate|redo|redraw|re-?draw|regen|try again)\b", low) or (
            re.search(r"\bgenerate\b", low) and (pages or secs or all_pages)):
        return Command("regenerate", pages=pages, sections=secs, providers=provs, all_pages=all_pages, raw=t)
    return None


# ------------------------------------------------------------------ execution

class Reply:
    def __init__(self):
        self.lines: list[str] = []
        self.actions: list[dict] = []

    def say(self, s: str) -> None:
        self.lines.append(s)

    def act(self, **kw) -> None:
        self.actions.append(kw)


def _pages_by_number(db: Session, project: Project, numbers: list[int]) -> list[Page]:
    q = select(Page).where(Page.project_id == project.id, Page.position.in_(numbers)).order_by(Page.position)
    return list(db.scalars(q))


def _section(db: Session, project: Project, n: int) -> Section | None:
    return db.scalars(select(Section).where(Section.project_id == project.id, Section.position == n)).first()


def _resolve_targets(db: Session, project: Project, cmd: Command) -> list[Page]:
    if cmd.pages:
        return _pages_by_number(db, project, cmd.pages)
    if cmd.sections:
        out = []
        for n in cmd.sections:
            s = _section(db, project, n)
            if s:
                out += list(db.scalars(select(Page).where(Page.section_id == s.id).order_by(Page.position)))
        return out
    if cmd.all_pages:
        return list(db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.position)))
    return []


def _provider_override(db: Session, providers: list[str], reply: Reply) -> tuple[str, str] | None:
    if not providers:
        return None
    pid = providers[0]
    if not registry.is_usable(db, pid):
        reply.say(f"⚠️ {pid} is not configured or is disabled — add its API key in the AI Model Control Center.")
        raise _Abort()
    model = registry.get_config(db, pid).default_model or registry.get_provider(db, pid).list_models()[0].model_id
    return pid, model


class _Abort(Exception):
    pass


def _pending(db: Session, project: Project) -> dict | None:
    last = db.scalars(select(ChatMessage).where(ChatMessage.project_id == project.id, ChatMessage.role == "assistant")
                      .order_by(ChatMessage.created_at.desc())).first()
    if last:
        for a in last.actions or []:
            if a.get("type") == "pending_confirmation":
                return a
    return None


def handle_message(db: Session, project: Project, message: str) -> ChatMessage:
    db.add(ChatMessage(project_id=project.id, role="user", content=message))
    db.flush()
    reply = Reply()
    cmds = parse(message)
    interpreted_by = "parser"
    context_pages = _context_pages(db, project)
    for c in cmds:
        if c.intent in PAGE_INTENTS and not (c.pages or c.sections or c.all_pages) and context_pages:
            c.pages = context_pages[:1] if c.intent in ("compare", "change_subject", "set_caption", "restore") \
                else context_pages
    if not cmds and llm.available(db):
        cmds = _llm_parse(db, project, message)
        interpreted_by = "text-model"
    if not cmds:
        reply.say("I couldn't map that to a book edit. Try e.g. “Regenerate page 14 using OpenAI”, “Make the lines "
                  "thicker on pages 6–12”, “Rewrite the caption on page 9”, or “help”.")
    for cmd in cmds:
        try:
            execute(db, project, cmd, reply)
        except _Abort:
            pass
        except (services.ConflictError, ValueError, costs.BudgetExceeded, costs.UnknownCost) as e:
            reply.say(f"⚠️ {e}")
    msg = ChatMessage(project_id=project.id, role="assistant", content="\n".join(reply.lines),
                      actions=reply.actions + [{"type": "interpreted", "by": interpreted_by,
                                                "commands": [c.to_dict() for c in cmds]}])
    db.add(msg)
    db.flush()
    return msg


PAGE_INTENTS = {"regenerate", "compare", "line_weight", "rewrite_caption", "approve", "reject", "lock", "unlock",
                "restore"}


def _context_pages(db: Session, project: Project) -> list[int]:
    """Pages referenced most recently in this conversation ("the same illustration")."""
    msgs = db.scalars(select(ChatMessage).where(ChatMessage.project_id == project.id, ChatMessage.role == "assistant")
                      .order_by(ChatMessage.created_at.desc()).limit(10))
    for m in msgs:
        for a in m.actions or []:
            if a.get("type") == "interpreted":
                for c in a.get("commands", []):
                    if c.get("pages"):
                        return c["pages"]
    return []


def _llm_parse(db: Session, project: Project, message: str) -> list[Command]:
    schema = ("Return {\"commands\": [{\"intent\": one of [regenerate, compare, line_weight, rewrite_caption, "
              "set_caption, change_subject, section_instructions, generate_next, approve, reject, lock, unlock, "
              "lock_approved, set_provider, undo, restore], \"pages\": [int], \"sections\": [int], "
              "\"providers\": [openai|google|bfl|stability|replicate|fal|mock|mock_alt], \"text\": str, "
              "\"count\": int, \"steps\": int, \"all_pages\": bool}]}")
    out = llm.complete_json(db, "You translate coloring-book editing requests into structured commands. " + schema,
                            f"Book has {project.illustration_count} pages.\nRequest: {message}")
    cmds = []
    for c in (out or {}).get("commands", []) if isinstance(out, dict) else []:
        if c.get("intent") in {"regenerate", "compare", "line_weight", "rewrite_caption", "set_caption",
                               "change_subject", "section_instructions", "generate_next", "approve", "reject",
                               "lock", "unlock", "lock_approved", "set_provider", "undo", "restore"}:
            cmds.append(Command(c["intent"], pages=[int(x) for x in c.get("pages", []) if str(x).isdigit()],
                                sections=[int(x) for x in c.get("sections", []) if str(x).isdigit()],
                                providers=[p for p in c.get("providers", []) if p in registry.PROVIDER_CLASSES],
                                text=str(c.get("text", ""))[:500], count=int(c.get("count") or 0),
                                steps=max(-3, min(3, int(c.get("steps") or 0))), all_pages=bool(c.get("all_pages")),
                                raw=message))
    return cmds


def execute(db: Session, project: Project, cmd: Command, reply: Reply, *, confirmed: bool = False) -> None:
    fn = EXECUTORS.get(cmd.intent)
    if fn is None:
        reply.say(f"Unsupported command: {cmd.intent}")
        return
    fn(db, project, cmd, reply, confirmed)


def _need_confirm(reply: Reply, cmd: Command, why: str) -> None:
    reply.say(f"{why} Reply “confirm” to proceed or “cancel”.")
    reply.act(type="pending_confirmation", command=cmd.to_dict(), reason=why)


def ex_confirm(db, project, cmd, reply, confirmed):
    p = _pending(db, project)
    if not p:
        reply.say("There is nothing waiting for confirmation.")
        return
    c = p["command"]
    cmd2 = Command(**{k: v for k, v in c.items() if k in Command.__dataclass_fields__})
    execute(db, project, cmd2, reply, confirmed=True)


def ex_decline(db, project, cmd, reply, confirmed):
    reply.say("OK — nothing was changed." if _pending(db, project) else "OK.")


def ex_help(db, project, cmd, reply, confirmed):
    reply.say("I can: regenerate pages (optionally with a provider), compare providers on a page, make lines "
              "thicker/thinner, rewrite or set captions, change what a page shows, adjust a section's instructions, "
              "generate the next N illustrations, approve/reject/lock/unlock pages, keep approved pages locked, "
              "choose a provider for a section, undo, and restore earlier revisions.")


def ex_regenerate(db, project, cmd, reply, confirmed):
    targets = _resolve_targets(db, project, cmd)
    if not targets:
        reply.say("Which pages? e.g. “regenerate page 14” or “regenerate section 2”.")
        return
    locked = [p.position for p in targets if p.locked]
    approved = [p.position for p in targets if not p.locked and p.status in services.APPROVED_STATES]
    run = [p for p in targets if not p.locked]
    if locked:
        reply.say(f"🔒 Skipping locked page(s): {locked}.")
    if approved and not confirmed:
        _need_confirm(reply, cmd, f"Page(s) {approved} are approved; regenerating creates new artwork for review "
                                  "(old versions stay in history).")
        return
    if not run:
        return
    override = _provider_override(db, cmd.providers, reply)
    est = jobs.estimate_pages(db, project, run) if not override else _estimate_override(db, project, run, override)
    if est is not None and est > CONFIRM_COST_USD and not confirmed:
        _need_confirm(reply, cmd, f"This will cost about ${est:.2f}.")
        return
    costs.check_budget(db, project, est, allow_unknown=confirmed)
    if est is None and project.budget_limit is not None and not confirmed:
        _need_confirm(reply, cmd, "The provider's price is unknown.")
        return
    job = jobs.enqueue(db, project.id, "generate_batch",
                       {"page_ids": [p.id for p in run], "override": list(override) if override else None,
                        "allow_approved": True, "allow_unknown_cost": confirmed, "source": "chat"},
                       estimated_cost=est)
    who = f" with {override[0]}/{override[1]}" if override else ""
    reply.say(f"Queued regeneration of page(s) {[p.position for p in run]}{who}. Other pages are untouched.")
    reply.act(type="job", job_id=job.id, job_type="generate_batch")


def _estimate_override(db, project, pages, override):
    from .providers.base import GenerationRequest
    prov = registry.get_provider(db, override[0])
    tw, th = services.target_pixels(project)
    e = prov.estimate_cost(GenerationRequest(project.id, None, None, override[0], override[1], "", width=tw, height=th))
    return None if e.amount is None else round(e.amount * len(pages), 4)


def ex_compare(db, project, cmd, reply, confirmed):
    pages = _pages_by_number(db, project, cmd.pages[:1])
    if not pages:
        reply.say("Which page should I compare? e.g. “try page 5 in FLUX and OpenAI”.")
        return
    page = pages[0]
    provs = list(cmd.providers)
    if len(provs) < 2:
        cur = None
        if page.current_revision_id:
            from .models import Revision
            r = db.get(Revision, page.current_revision_id)
            cur = r.provider_id if r and r.provider_id in registry.PROVIDER_CLASSES else None
        base = cur or project.default_provider
        if base not in provs:
            provs.insert(0, base)
    usable = [p for p in provs if registry.is_usable(db, p)]
    missing = [p for p in provs if p not in usable]
    if missing:
        reply.say(f"⚠️ Not configured: {missing}.")
    if len(usable) < 2:
        reply.say("A comparison needs at least two configured providers.")
        return
    contenders = [{"provider_id": p} for p in usable]
    b = battle.create(db, project, page, contenders, allow_unknown_cost=confirmed)
    reply.say(f"Started a model battle for page {page.position}: {', '.join(usable)}. The current artwork stays "
              "until you pick a winner in AI Model Battle.")
    reply.act(type="battle", battle_id=b.id, job_id=b.job_id)


def ex_line_weight(db, project, cmd, reply, confirmed):
    targets = _resolve_targets(db, project, cmd)
    if not targets:
        reply.say("Which pages? e.g. “make the lines thicker on pages 6–12”.")
        return
    locked = [p.position for p in targets if p.locked]
    if locked:
        reply.say(f"🔒 Skipping locked page(s): {locked}.")
    pending = ("queued", "generating")
    # pages still being generated are included: the edit job is queued behind the generation job
    run = [p for p in targets if not p.locked and (p.current_revision_id or p.status in pending)]
    no_art = [p.position for p in targets if not p.locked and not p.current_revision_id and p.status not in pending]
    if no_art:
        reply.say(f"Page(s) {no_art} have no artwork yet; they'll use the book style when generated.")
    if not run:
        return
    job = jobs.enqueue(db, project.id, "line_weight", {"page_ids": [p.id for p in run], "steps": cmd.steps,
                                                       "source": "chat"})
    word = "thicker" if cmd.steps > 0 else "thinner"
    reply.say(f"Queued: lines {word} on page(s) {[p.position for p in run]} (local edit, no API cost).")
    reply.act(type="job", job_id=job.id, job_type="line_weight")


def ex_rewrite_caption(db, project, cmd, reply, confirmed):
    targets = _resolve_targets(db, project, cmd)
    if not targets:
        reply.say("Which page's caption? e.g. “rewrite the caption on page 9”.")
        return
    for p in targets:
        if p.locked:
            reply.say(f"🔒 Page {p.position} is locked; caption unchanged.")
            continue
        before = services.snapshot(p)
        text = _rewrite(db, project, p, cmd.text)
        res = services.set_caption(db, project, p, text, source="chat")
        services.log_edit(db, project.id, "caption", page=p, before=before, source="chat")
        status = "✅" if res["passed"] else "⚠️ check fit"
        reply.say(f"Page {p.position} caption {status}: “{text}”")
        reply.act(type="caption", page=p.position)


def _rewrite(db: Session, project: Project, page: Page, style: str) -> str:
    out = llm.complete_json(db, "You write short, accurate captions for children's coloring pages. Return {\"caption\": str}. "
                            "Never suggest unsafe activities.",
                            f"Audience age {project.audience_age}. Subject: {page.subject}. Fact: {page.fact}. "
                            f"Current caption: {page.caption_text}. Style request: {style or 'fresh wording'}. Max 170 chars.")
    if isinstance(out, dict) and out.get("caption"):
        return str(out["caption"])[:300]
    st = captions.settings_for(project.caption_settings)
    if style == "question" or (not style and page.caption_text and page.fact and page.fact in page.caption_text):
        st.update(question=True, explanation=True)
    if style == "vocabulary":
        st["vocabulary"] = True
    if style == "shorter" or style == "simpler":
        first = re.split(r"(?<=[.!?])\s+", page.fact or page.caption_text)[0]
        return first
    text = captions.compose(page, st)
    if text == page.caption_text:
        st.update(vocabulary=True, question=True)
        text = captions.compose(page, st)
    return text


def ex_set_caption(db, project, cmd, reply, confirmed):
    pages = _pages_by_number(db, project, cmd.pages)
    if not pages:
        reply.say("Page not found.")
        return
    p = pages[0]
    before = services.snapshot(p)
    res = services.set_caption(db, project, p, cmd.text, source="chat")
    services.log_edit(db, project.id, "caption", page=p, before=before, source="chat")
    bad = [c["message"] for c in res["checks"] if c["status"] != "pass"]
    reply.say(f"Page {p.position} caption updated (artwork untouched)." + (f" Notes: {'; '.join(bad)}" if bad else ""))


def ex_change_subject(db, project, cmd, reply, confirmed):
    pages = _pages_by_number(db, project, cmd.pages)
    if not pages:
        reply.say("Page not found.")
        return
    p = pages[0]
    if p.locked:
        reply.say(f"🔒 Page {p.position} is locked.")
        return
    if p.status in services.APPROVED_STATES and not confirmed:
        _need_confirm(reply, cmd, f"Page {p.position} is approved.")
        return
    before = services.snapshot(p)
    p.subject = cmd.text[:1].upper() + cmd.text[1:]
    p.concept = cmd.text
    p.prompt_version += 1
    p.status = "revision_requested"
    p.caption_status = "review"
    p.version += 1
    services.log_edit(db, project.id, "change_subject", page=p, before=before, source="chat",
                      details={"new_subject": cmd.text})
    reply.say(f"Page {p.position} will now show {cmd.text}. Its caption is flagged for review.")
    ex_regenerate(db, project, Command("regenerate", pages=[p.position], providers=cmd.providers), reply, True)


def ex_section_instructions(db, project, cmd, reply, confirmed):
    s = _section(db, project, cmd.sections[0])
    if not s:
        reply.say("Section not found.")
        return
    s.theme_instructions = (s.theme_instructions + " " if s.theme_instructions else "") + f"Make it {cmd.text}."
    s.version += 1
    pages = list(db.scalars(select(Page).where(Page.section_id == s.id)))
    affected = [p.position for p in pages if not p.locked and p.status not in services.APPROVED_STATES]
    services.log_edit(db, project.id, "section_instructions", source="chat",
                      details={"section": s.position, "instructions": s.theme_instructions})
    reply.say(f"Section {s.position} “{s.name}” instructions updated. New and regenerated pages will be {cmd.text}. "
              f"Unapproved pages that would change: {affected or 'none'}. Say “regenerate section {s.position}” to "
              "apply now (approved/locked pages are kept).")


def ex_generate_next(db, project, cmd, reply, confirmed):
    pages = jobs.next_batch_pages(db, project, cmd.count or project.batch_size)
    if not pages:
        reply.say("Every page already has artwork or is queued.")
        return
    override = _provider_override(db, cmd.providers, reply)
    est = jobs.estimate_pages(db, project, pages) if not override else _estimate_override(db, project, pages, override)
    if est is not None and est > CONFIRM_COST_USD and not confirmed:
        _need_confirm(reply, cmd, f"Generating {len(pages)} pages will cost about ${est:.2f}.")
        return
    costs.check_budget(db, project, est, allow_unknown=confirmed)
    job = jobs.enqueue(db, project.id, "generate_batch", {"page_ids": [p.id for p in pages],
                                                          "override": list(override) if override else None,
                                                          "allow_unknown_cost": confirmed,
                                                          "autopilot": project.autopilot},
                       estimated_cost=est)
    reply.say(f"Queued the next {len(pages)} illustration(s): pages {[p.position for p in pages]}"
              + (f" (≈${est:.2f})" if est else "") + ".")
    reply.act(type="job", job_id=job.id, job_type="generate_batch")


def _set_flag(db, project, cmd, reply, action: str):
    targets = _resolve_targets(db, project, cmd)
    if not targets:
        reply.say(f"Which pages should I {action}?")
        return
    changed = []
    for p in targets:
        before = services.snapshot(p)
        if action == "approve":
            if p.locked or not p.current_revision_id:
                continue
            p.status = "approved"
            if p.caption_status == "review":
                p.caption_status = "ok"
        elif action == "lock":
            if not p.current_revision_id or p.locked:
                continue
            p.locked = True
            p.status = "locked"
        elif action == "unlock":
            if not p.locked:
                continue
            p.locked = False
            p.status = "approved"
        elif action == "reject":
            if p.locked:
                continue
            p.status = "revision_requested"
        p.version += 1
        services.log_edit(db, project.id, action, page=p, before=before, source="chat")
        changed.append(p.position)
    reply.say(f"{action.capitalize()}d page(s): {changed or 'none (locked, empty or unchanged)'}.")


def ex_approve(db, project, cmd, reply, confirmed):
    _set_flag(db, project, cmd, reply, "approve")


def ex_lock(db, project, cmd, reply, confirmed):
    _set_flag(db, project, cmd, reply, "lock")


def ex_unlock(db, project, cmd, reply, confirmed):
    _set_flag(db, project, cmd, reply, "unlock")


def ex_reject(db, project, cmd, reply, confirmed):
    _set_flag(db, project, cmd, reply, "reject")


def ex_lock_approved(db, project, cmd, reply, confirmed):
    pages = list(db.scalars(select(Page).where(Page.project_id == project.id, Page.status.in_(["approved", "export_ready"]))))
    for p in pages:
        before = services.snapshot(p)
        p.locked = True
        p.status = "locked"
        p.version += 1
        services.log_edit(db, project.id, "lock", page=p, before=before, source="chat")
    reply.say(f"Locked {len(pages)} approved page(s); no command can change them until unlocked.")


def ex_set_provider(db, project, cmd, reply, confirmed):
    if cmd.sections:
        secs = [_section(db, project, n) for n in cmd.sections]
    else:  # "next section": first section that still has pages without artwork
        secs = []
        for s in db.scalars(select(Section).where(Section.project_id == project.id).order_by(Section.position)):
            if any(not p.current_revision_id for p in db.scalars(select(Page).where(Page.section_id == s.id))):
                secs = [s]
                break
    secs = [s for s in secs if s]
    if not secs:
        reply.say("No matching section.")
        return
    if cmd.providers:
        pid = cmd.providers[0]
        if not registry.is_usable(db, pid):
            reply.say(f"⚠️ {pid} is not configured.")
            return
        model = registry.get_config(db, pid).default_model or registry.get_provider(db, pid).list_models()[0].model_id
        why = "requested"
    else:
        pref = cmd.text if cmd.text in routing.WEIGHTS else "highest_quality"
        tw, th = services.target_pixels(project)
        ranked = routing.rank(db, project, max(tw, th), preference=pref)
        if not ranked:
            reply.say("No configured providers to choose from.")
            return
        pid, model, why = ranked[0].provider_id, ranked[0].model_id, ranked[0].reason
    for s in secs:
        s.preferred_provider, s.preferred_model = pid, model
        s.version += 1
        services.log_edit(db, project.id, "section_provider", source="chat",
                          details={"section": s.position, "provider": pid, "model": model})
    reply.say(f"Section(s) {[s.position for s in secs]} will use {pid}/{model} ({why}).")


def ex_undo(db, project, cmd, reply, confirmed):
    page_id = None
    if cmd.pages:
        ps = _pages_by_number(db, project, cmd.pages[:1])
        page_id = ps[0].id if ps else None
    e = services.undo_last(db, project.id, page_id)
    if e is None:
        reply.say("Nothing to undo.")
    else:
        pg = db.get(Page, e.page_id)
        reply.say(f"Undid “{e.action}” on page {pg.position}.")


def ex_restore(db, project, cmd, reply, confirmed):
    from .models import Revision
    pages = _pages_by_number(db, project, cmd.pages[:1])
    if not pages:
        reply.say("Page not found.")
        return
    p = pages[0]
    if p.locked:
        reply.say(f"🔒 Page {p.position} is locked.")
        return
    cur = db.get(Revision, p.current_revision_id) if p.current_revision_id else None
    target = None
    if cmd.text in ("previous", "original") and cur:
        revs = list(db.scalars(select(Revision).where(Revision.page_id == p.id, Revision.kind.in_(
            ["generated", "enhanced", "edited"]), Revision.id != cur.id).order_by(Revision.created_at.desc())))
        if cmd.text == "original":
            revs = revs[::-1]
        target = revs[0] if revs else None
    else:
        target = db.scalars(select(Revision).where(Revision.page_id == p.id, Revision.id.startswith(cmd.text))).first()
    if target is None:
        reply.say("No earlier revision found.")
        return
    before = services.snapshot(p)
    p.current_revision_id = target.id
    p.status = "awaiting_review"
    p.version += 1
    services.log_edit(db, project.id, "restore", page=p, before=before, source="chat", details={"revision": target.id})
    reply.say(f"Page {p.position} restored to revision {target.id[:8]} ({target.kind}, {target.provider_id}).")


EXECUTORS = {
    "confirm": ex_confirm, "decline": ex_decline, "help": ex_help, "regenerate": ex_regenerate,
    "compare": ex_compare, "line_weight": ex_line_weight, "rewrite_caption": ex_rewrite_caption,
    "set_caption": ex_set_caption, "change_subject": ex_change_subject,
    "section_instructions": ex_section_instructions, "generate_next": ex_generate_next, "approve": ex_approve,
    "lock": ex_lock, "unlock": ex_unlock, "reject": ex_reject, "lock_approved": ex_lock_approved,
    "set_provider": ex_set_provider, "undo": ex_undo, "restore": ex_restore,
}
