#!/usr/bin/env python3
# 造 Banxa 转化漏斗 sandbox 数据：14 天 × 国家 × 支付方式 × 漏斗阶段。
# 在「昨天」(2026-06-27) 埋一个真实可归因的下跌：visa 的 提交→授权 通过率从 0.85 崩到 0.45
# （模拟发卡行/3DS 授权失败激增），把当天整体转化率从 ~67% 拖到 ~50%。
# 分析 Agent 应能定位到：跌幅集中在 pay_authorized 阶段、且集中在 visa。
import sqlite3, os

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banxa.db")
if os.path.exists(DB):
    os.remove(DB)

DAYS = [f"2026-06-{d:02d}" for d in range(14, 28)]   # 06-14 .. 06-27（昨天=06-27）
INCIDENT_DAY = "2026-06-27"
COUNTRIES = {"SG": 0.30, "AU": 0.25, "HK": 0.20, "JP": 0.15, "GB": 0.10}
METHODS   = {"visa": 0.55, "mastercard": 0.35, "applepay": 0.10}
STAGES = ["created", "kyc_passed", "pay_submitted", "pay_authorized", "completed"]

# 正常逐级通过率
R = {"kyc_passed": 0.92, "pay_submitted": 0.88, "pay_authorized": 0.85, "completed": 0.98}

def daily_total(i):           # 轻微的日波动（确定性，无随机依赖）
    return 4800 + (i % 7) * 120

rows = []
for i, ds in enumerate(DAYS):
    total = daily_total(i)
    for c, cw in COUNTRIES.items():
        for m, mw in METHODS.items():
            created = round(total * cw * mw)
            # 逐级流转
            kyc = round(created * R["kyc_passed"])
            sub = round(kyc * R["pay_submitted"])
            # 埋点：昨天 visa 的 提交→授权 通过率崩盘
            r_auth = 0.45 if (ds == INCIDENT_DAY and m == "visa") else R["pay_authorized"]
            auth = round(sub * r_auth)
            comp = round(auth * R["completed"])
            stage_users = {
                "created": created, "kyc_passed": kyc, "pay_submitted": sub,
                "pay_authorized": auth, "completed": comp,
            }
            for s in STAGES:
                rows.append((ds, c, m, s, stage_users[s]))

con = sqlite3.connect(DB)
con.execute("""CREATE TABLE banxa_funnel(
    ds TEXT, country TEXT, pay_method TEXT, stage TEXT, users INTEGER)""")
con.executemany("INSERT INTO banxa_funnel VALUES (?,?,?,?,?)", rows)
con.commit()

# 自检：打印每日整体转化率，确认下跌可见且可归因
print(f"已写入 {len(rows)} 行 → {DB}\n按日整体转化率(completed/created):")
q = """
SELECT ds,
       SUM(CASE WHEN stage='created'   THEN users END) AS created,
       SUM(CASE WHEN stage='completed' THEN users END) AS completed
FROM banxa_funnel GROUP BY ds ORDER BY ds"""
for ds, cr, cp in con.execute(q):
    print(f"  {ds}  created={cr:5d}  completed={cp:5d}  conv={cp/cr*100:5.1f}%")
con.close()
