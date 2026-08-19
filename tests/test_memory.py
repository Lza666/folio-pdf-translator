from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.memory import exact_match, fuzzy_matches, upsert_memory


def test_translation_memory_version_and_fuzzy_match():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        unit = upsert_memory(
            db,
            source_language="en",
            target_language="zh-Hans",
            source_text="The quick brown fox jumps over the fence.",
            target_text="敏捷的棕色狐狸跳过栅栏。",
        )
        db.commit()
        assert exact_match(db, "en", "zh-Hans", "The quick brown fox jumps over the fence.") == "敏捷的棕色狐狸跳过栅栏。"
        upsert_memory(
            db,
            source_language="en",
            target_language="zh-Hans",
            source_text="The quick brown fox jumps over the fence.",
            target_text="那只敏捷的棕狐跃过栅栏。",
        )
        db.commit()
        assert unit.version == 2
        matches = fuzzy_matches(
            db, "en", "zh-Hans", "The quick brown fox jumps over the fence!"
        )
        assert matches and matches[0]["score"] >= 92

