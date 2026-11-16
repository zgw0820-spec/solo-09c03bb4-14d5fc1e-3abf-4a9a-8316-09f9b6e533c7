"""测试夹具：每个用例使用独立临时数据库。

注意：app.main 在导入时会创建模块级 app（uvicorn 使用），
这里先把 DB_PATH 指到临时目录，避免污染仓库。
"""
import os
import tempfile

os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "coldchain_module_default.db"))
os.environ.setdefault("WORKER_ENABLED", "0")
