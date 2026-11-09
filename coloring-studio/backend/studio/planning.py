"""Section recommendations, educational progression and page planning."""
from __future__ import annotations

import math

RECOMMENDED = {10: (1, 1), 20: (2, 2), 30: (3, 4), 40: (4, 5), 60: (6, 8), 80: (8, 10), 100: (10, 12)}
PROGRESSION = ["introduction", "tools", "processes", "applications", "careers"]

VARIANTS = [
    ("{s}", "{scene}"),
    ("{s}: a closer look", "a close-up view of the important parts of: {scene}"),
    ("{s}: learning together", "two children and a friendly guide learning about it — {scene}"),
    ("{s}: the big picture", "a wide scene showing the setting around it — {scene}"),
    ("{s}: step by step", "a simple three-step picture sequence in one image explaining it — {scene}"),
]


def recommend_sections(n: int, education: bool = True) -> dict:
    if n in RECOMMENDED:
        lo, hi = RECOMMENDED[n]
    else:
        keys = sorted(RECOMMENDED)
        if n < keys[0]:
            lo = hi = 1
        elif n > keys[-1]:
            lo, hi = math.ceil(n / 10), math.ceil(n / 8)
        else:
            k = max(x for x in keys if x <= n)
            lo, hi = RECOMMENDED[k]
    rec = 5 if (education and lo <= 5 <= hi) else lo
    return {"min": lo, "max": hi, "recommended": rec}


def allocate(total: int, k: int) -> list[int]:
    if k <= 0:
        raise ValueError("need at least one section")
    if total < k:
        raise ValueError("more sections than illustrations")
    base, extra = divmod(total, k)
    return [base + (1 if i < extra else 0) for i in range(k)]


def map_stages(stages: list[dict], k: int) -> list[dict]:
    """Map research stages onto k sections (merge or split), keeping learning order."""
    n = len(stages)
    out = []
    if k <= n:
        groups = allocate(n, k)
        i = 0
        for g in groups:
            grp = stages[i:i + g]
            i += g
            out.append({"name": " & ".join(s["name"] for s in grp), "stage": grp[0]["stage"],
                        "stage_indices": list(range(i - g, i)), "goals": " ".join(s["goals"] for s in grp)})
    else:
        parts = allocate(k, n)
        for idx, (s, p) in enumerate(zip(stages, parts)):
            for j in range(p):
                out.append({"name": s["name"] + (f" — Part {j + 1}" if p > 1 else ""), "stage": s["stage"],
                            "stage_indices": [idx], "part": (j, p), "goals": s["goals"]})
    return out


def subjects_for_section(subjects: list[dict], sec: dict, count: int, used: set[str]) -> list[dict]:
    pool = [s for s in subjects if s.get("stage") in sec["stage_indices"]]
    if "part" in sec:
        j, p = sec["part"]
        size = math.ceil(len(pool) / p) if pool else 0
        pool = pool[j * size:(j + 1) * size] or pool
    if not pool:
        pool = list(subjects)
    chosen: list[dict] = []
    v = 0
    while len(chosen) < count:
        for s in pool:
            if len(chosen) >= count:
                break
            tpl_s, tpl_scene = VARIANTS[v % len(VARIANTS)]
            subj = tpl_s.format(s=s["subject"])
            if subj in used:
                continue
            used.add(subj)
            chosen.append({**s, "subject": subj, "scene": tpl_scene.format(scene=s["scene"]), "variant": v})
        v += 1
        if v > 50:  # pragma: no cover - safety valve
            raise RuntimeError("cannot create enough distinct page concepts")
    return chosen


def build_plan(total: int, k: int, research: dict, education: bool = True) -> list[dict]:
    stages = research["suggested_sections"]
    subjects = research["subjects"]
    if not education:
        stages = [{"name": f"Section {i + 1}", "stage": "mixed", "goals": ""} for i in range(k)]
        subjects = [dict(s, stage=i % k) for i, s in enumerate(subjects)]
    secs = map_stages(stages, k) if education else [
        {**s, "stage_indices": [i]} for i, s in enumerate(stages)]
    counts = allocate(total, k)
    used: set[str] = set()
    plan = []
    for sec, cnt in zip(secs, counts):
        plan.append({"name": sec["name"], "learning_stage": sec["stage"], "educational_goals": sec["goals"],
                     "illustration_count": cnt, "pages": subjects_for_section(subjects, sec, cnt, used)})
    assert sum(p["illustration_count"] for p in plan) == total
    return plan
