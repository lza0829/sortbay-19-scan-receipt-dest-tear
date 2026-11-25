"""
既有链路回归：确认缺目的地装载闸没有破坏路由裁决、错分回流、封袋、封班。
口门闭口/路由规则本身属独立需求，这里只做不回归验证。
"""
from __future__ import annotations


def test_完整快乐链路_装载到封班(client):
    # 正常件装载即进待路由桶
    rec = client.post("/扫描", json={"条码": "HAPPY-001", "目的地码": "110"})
    assert rec.status_code == 200
    sid = rec.json()["id"]
    assert client.get("/待路由桶").json()["件数"] >= 1

    # 路由裁决到 A01（种子路由 110→A01）
    routed = client.post(f"/扫描/{sid}/路由")
    assert routed.status_code == 200 and routed.json()["status"] == "已路由"
    chute_id = routed.json()["chute_id"]

    # 错分回流后可再裁决
    div = client.post("/回流批次", json={"扫描编号": sid})
    assert div.status_code == 200
    assert div.json()["扫描"]["status"] == "错分回流"
    rerouted = client.post(f"/扫描/{sid}/路由")
    assert rerouted.status_code == 200 and rerouted.json()["status"] == "已路由"

    # 清掉种子自带的待路由件（同为 110→A01），避免挡住封班
    for s in client.get("/扫描").json():
        if s["status"] == "待路由" and s.get("可裁决"):
            r = client.post(f"/扫描/{s['id']}/路由")
            assert r.status_code == 200

    # 开袋→把所有已路由且口门一致的件入袋→封口
    bag = client.post("/封袋/开袋", json={"口门编号": chute_id})
    assert bag.status_code == 200
    bid = bag.json()["id"]
    for s in client.get("/扫描").json():
        if s["status"] == "已路由" and s["chute_id"] == chute_id:
            assert client.post(f"/封袋/{bid}/入袋", json={"扫描编号": s["id"]}).status_code == 200
    sealed_bag = client.post(f"/封袋/{bid}/封口")
    assert sealed_bag.status_code == 200 and sealed_bag.json()["status"] == "已封袋"

    # 全部清完后封班成功
    shift_id = client.get("/班次").json()[0]["id"]
    seal = client.post(f"/班次/{shift_id}/封班")
    assert seal.status_code == 200 and seal.json()["sealed"] == 1

    # 封班后写口被禁
    blocked = client.post("/扫描", json={"条码": "AFTER-SEAL", "目的地码": "110"})
    assert blocked.status_code == 400
