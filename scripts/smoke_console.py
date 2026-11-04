"""真实 LLM + 真实写路径的端到端验证（库/目录指向临时副本，不动正式数据）。

跑法：python scripts/_live_console_check.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import config, db  # noqa: E402

TMP = ROOT / "data" / "_tmp_live"
if TMP.exists():
    shutil.rmtree(TMP)
for sub in ("kb", "inbox", "iv", "inbox/pending", "inbox/archived", "reports"):
    (TMP / sub).mkdir(parents=True, exist_ok=True)

shutil.copy(ROOT / "data" / "kb.sqlite", TMP / "kb.sqlite")
shutil.copytree(ROOT / "knowledge", TMP / "kb", dirs_exist_ok=True)

# 必须在 import 服务之前改：db 模块把 DB_PATH 绑进了自己的命名空间
db.DB_PATH = TMP / "kb.sqlite"
config.DB_PATH = TMP / "kb.sqlite"
config.KB_DIR = TMP / "kb"
config.INBOX_DIR = TMP / "inbox"
config.IV_DIR = TMP / "iv"
config.REPORTS_DIR = TMP / "reports"

from app.services import console_service  # noqa: E402

TEXT = """2026年11月3日 面 某电商平台 后端开发 一面

1. 你们订单表怎么做的分库分表？分片键为什么选 user_id，换成 order_id 会有什么问题？
2. 订单状态机怎么保证幂等？重复回调怎么处理？
3. Kafka 消费积压了几百万条，你怎么排查和处理？扩容消费者一定有效吗？
4. RocketMQ 的事务消息解决什么问题，跟本地消息表比优缺点？
5. 缓存和数据库双写，你们用先删缓存还是先更库？中间有不一致窗口吗？
"""


def main():
    before = _counts()
    res = console_service.chat(TEXT, mode="auto")
    after = _counts()

    print("=== reply ===")
    print(res["reply"])
    print("\n=== trace ===")
    for s in res["trace"]:
        print(f"  [{s['iter']}] {s['tool']}  ok={s['ok']}  args={json.dumps(s['args'], ensure_ascii=False)}")
        print(f"      -> {s['preview'][:260]}")
    print("\n=== artifacts ===", res["artifacts"])
    print("\n=== 数据变化 ===")
    for k in before:
        print(f"  {k}: {before[k]} -> {after[k]}  ({after[k] - before[k]:+d})")


def _counts():
    conn = db.get_conn()
    try:
        q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
        return {
            "ingests": q("SELECT COUNT(*) FROM ingests"),
            "feed_interviews": q("SELECT COUNT(*) FROM feed_interviews"),
            "question_pool": q("SELECT COUNT(*) FROM question_pool"),
            "pending_actions": q("SELECT COUNT(*) FROM agent_actions WHERE status='pending'"),
            "kb_cards": q("SELECT COUNT(*) FROM kb_cards"),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    main()
