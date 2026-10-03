# -*- coding: utf-8 -*-
"""初始化数据库（建表 + 演示档案）。幂等，可重复运行。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import Base, engine, SessionLocal
from app.core.migration import ensure_schema
from app.core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY
from app.models import GameSession, Resident, Facility
from app.services.engine import FACILITY_ZH


def main():
    Base.metadata.create_all(bind=engine)
    ensure_schema(engine)
    db = SessionLocal()
    try:
        # 若已有档案则不重复造演示数据
        if db.query(GameSession).first():
            print("数据库已存在档案，跳过演示数据。")
            return
        gs = GameSession(
            name="基地一号（演示）",
            day=1,
            target_day=SURVIVAL_TARGET_DAY,
            status="running",
            resources=dict(INITIAL_RESOURCES),
            survivors=3,
            score=0,
        )
        db.add(gs)
        db.flush()
        for name, job, hp, mp in (
            ("林粤", "engineer", 90, 80),
            ("夏岚", "farmer", 88, 85),
            ("老周", "general", 92, 78),
        ):
            db.add(Resident(session_id=gs.id, name=name, job=job, health=hp, morale=mp, alive=1, joined_day=1))
        for cat in ("power", "farm", "water", "oxygen"):
            db.add(Facility(session_id=gs.id, name=FACILITY_ZH[cat], category=cat, level=1, status="active", built_day=1))
        db.commit()
        print("演示档案创建完成。启动：npm start（或 uvicorn app.main:app --reload）")
    finally:
        db.close()


if __name__ == "__main__":
    main()