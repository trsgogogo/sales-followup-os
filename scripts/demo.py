"""Create synthetic records in a separate demo database; sends nothing."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from followup.core import Engine

path = sys.argv[1] if len(sys.argv) > 1 else "data/demo.db"
now = time.time()
engine = Engine(path, owners=["Alex", "Taylor"], clock=lambda: now - 4 * 86400)
buyer = engine.create({"name": "示例客户 A", "company": "示例软件公司", "email": "buyer-a@example.com", "external_id": "demo:a", "source": "网站咨询", "notes": "希望了解实施周期和服务范围，预算待确认。"})
engine.event(buyer["id"], {"kind": "quote", "text": "已发出基础方案报价，等待客户确认范围。", "idempotency_key": "demo:quote:a"})
engine.clock = lambda: now - 2 * 86400
buyer = engine.create({"name": "示例客户 B", "company": "示例咨询公司", "email": "buyer-b@example.com", "external_id": "demo:b", "source": "活动交流"})
engine.event(buyer["id"], {"kind": "meeting", "text": "客户关注交付方式，尚未约定下一次沟通。", "idempotency_key": "demo:meeting:b"})
engine.clock = lambda: now - 20 * 60
engine.create({"name": "示例客户 C", "company": "示例制造企业", "external_id": "demo:c", "source": "网站表单", "notes": "询问服务介绍，尚未联系。"})
engine.clock = time.time
print(engine.tick())
print("Synthetic demo database:", path)
engine.db.close()
