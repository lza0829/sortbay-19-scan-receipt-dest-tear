"""HTTP 端到端冒烟：真实 uvicorn 下验证缺码装载链路同刷。"""
import sys
import httpx

BASE = "http://127.0.0.1:11699/api"
c = httpx.Client(base_url=BASE, timeout=30)

# 健康
assert c.get("/健康").status_code == 200

# 1. 缺目的地装载
r = c.post("/扫描", json={"条码": "SMOKE-NO-DST", "目的地码": ""})
r.raise_for_status()
rec = r.json()
assert rec["装载结果"] == "待补码" and rec["可裁决"] is False, rec
assert rec["status"] == "待补目的地" and rec["拒因"] == "缺目的地码"
sid = rec["id"]
print("1 缺码装载回执:", rec["装载结果"], rec["status"], rec["拒因"])

# 2. 待路由桶不含、待补码视图含、总览待补+1待路由不涨、审计有缺码
bucket = c.get("/待路由桶").json()
assert all(x["barcode"] != "SMOKE-NO-DST" for x in bucket["明细"])
need = c.get("/扫描/待补码").json()
assert any(x["id"] == sid for x in need)
ov = c.get("/总览").json()
assert ov["分布"]["待补目的地"] >= 1 and ov["脏行数"] == 0, ov
au = c.get("/审计").json()
assert any(a["ref_id"] == sid and "缺目的地码" in a["note"] for a in au)
print("2 桶/待补视图/总览/审计 同刷 OK；待补目的地 =", ov["分布"]["待补目的地"])

# 3. 补码前裁决被硬挡
blocked = c.post(f"/扫描/{sid}/路由")
assert blocked.status_code == 400 and "缺目的地码" in blocked.json()["detail"]
print("3 补码前裁决硬挡:", blocked.json()["detail"])

# 4. 补码后进桶、可裁决
sup = c.post(f"/扫描/{sid}/补码", json={"目的地码": "110"})
sup.raise_for_status()
assert sup.json()["status"] == "待路由" and sup.json()["可裁决"] is True
bucket2 = c.get("/待路由桶").json()
assert any(x["id"] == sid for x in bucket2["明细"])
routed = c.post(f"/扫描/{sid}/路由")
routed.raise_for_status()
assert routed.json()["status"] == "已路由"
print("4 补码后裁决成功 ->", routed.json()["status"])

# 5. 同条码再装被班次唯一拒绝
dup = c.post("/扫描", json={"条码": "SMOKE-NO-DST", "目的地码": "210"})
assert dup.status_code == 400 and "重复" in dup.json()["detail"]
print("5 双装拒绝:", dup.json()["detail"])

# 6. 正常件全链路
ok = c.post("/扫描", json={"条码": "SMOKE-OK", "目的地码": "210"}).json()
assert ok["装载结果"] == "成功" and ok["可裁决"] is True
ov2 = c.get("/总览").json()
assert ov2["脏行数"] == 0
print("6 正常件装载成功；总览分布:", ov2["分布"])

print("SMOKE ALL PASS")
sys.exit(0)
