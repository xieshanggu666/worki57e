# -*- coding: utf-8 -*-
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .core.database import Base, engine
from .core.migration import ensure_schema
from .api.router import router

BASE = Path(__file__).resolve().parent.parent
STATIC = BASE / "static"

app = FastAPI(title="末日地堡生存", version="1.0.0")

# 先按最新模型补缺表，再为旧版数据库补齐新增列（幂等，兼容旧档案）
Base.metadata.create_all(bind=engine)
ensure_schema(engine)

app.include_router(router)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")