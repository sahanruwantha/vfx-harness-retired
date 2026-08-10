"""Desk memory — ShotContext accumulation/curation + LessonBook seed/harvest/dedup/persist."""

from __future__ import annotations

from agents.desk_memory import (
    SEED_LESSONS,
    LessonBook,
    ShotContext,
    harvest_lessons,
)


# --- ShotContext --------------------------------------------------------------------


def test_shot_context_empty_renders_nothing():
    assert ShotContext().render() == ""


def test_shot_context_accumulates_and_saves(tmp_path):
    ctx = ShotContext(path=tmp_path / "context.md")
    ctx.set_intent("green tower barrel-roll to purple 2.0")
    ctx.add_decision("modeling", "camera at (0,-430,62) lens 30 — low hero angle; do not move")
    ctx.add_decision("look-dev", "green crown uses m_green strength 6")
    ctx.set_open_issues("sign text unreadable")
    rendered = ctx.render()
    assert "PROJECT CONTEXT" in rendered
    assert "green tower barrel-roll" in rendered
    assert "[modeling]" in rendered and "[look-dev]" in rendered and "m_green" in rendered
    assert "sign text unreadable" in rendered
    # persisted to disk for inspection
    assert "m_green" in (tmp_path / "context.md").read_text()


def test_shot_context_caps_decisions():
    ctx = ShotContext()
    for i in range(40):
        ctx.add_decision("s", f"decision {i}")
    assert len(ctx.decisions) <= 15
    assert "decision 39" in ctx.render() and "decision 0" not in ctx.render()


# --- LessonBook ---------------------------------------------------------------------


def test_harvest_lessons_pulls_lesson_lines():
    text = "SUBMIT: built the tower.\nLESSON: scene.eevee has no use_bloom in 5.x\n- LESSON: **foo moved to bar**"
    found = harvest_lessons(text)
    assert "scene.eevee has no use_bloom in 5.x" in found
    assert "foo moved to bar" in found


def test_lessonbook_seeds_and_renders():
    book = LessonBook()
    rendered = book.render()
    assert "LESSONS" in rendered
    assert any(seed in rendered for seed in SEED_LESSONS)


def test_lessonbook_harvest_adds_novel_and_dedupes(tmp_path):
    book = LessonBook(path=tmp_path / "lessons.md")
    added = book.harvest_and_add("LESSON: mesh.from_pydata needs update() after")
    assert added == ["mesh.from_pydata needs update() after"]
    # a duplicate (even reworded casing) is not re-added
    again = book.harvest_and_add("LESSON: MESH.FROM_PYDATA needs update() after")
    assert again == []
    # a seeded gotcha is not re-added as novel
    assert book.harvest_and_add("LESSON: EEVEE's engine id is 'BLENDER_EEVEE'") == []
    # persisted across instances
    reloaded = LessonBook(path=tmp_path / "lessons.md")
    assert any("from_pydata" in h for h in reloaded.harvested)
    assert "from_pydata" in reloaded.render()
