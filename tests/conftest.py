# -*- coding: utf-8 -*-
"""pytest 全局配置：在导入任何 app 模块之前，把数据库切到独立临时文件，
避免测试清表（drop_all）误伤 data/bunker.db 里的本地存档。"""
import os
import tempfile

_TEST_DB_DIR = tempfile.mkdtemp(prefix="bunker-test-")
os.environ["BUNKER_DATABASE_URL"] = f"sqlite:///{_TEST_DB_DIR}/test.db"
