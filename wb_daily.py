#!/usr/bin/env python3
"""WorkBuddy 每日签到 + 派猫猫旅行（自包含精简版，标准库实现）。
用法: python wb_daily.py <tokens.json>
tokens.json: [{"uid":"...","accessToken":"..."}, ...]
输出: JSON 数组，每账号一条 {uid, status, action, points, balance, streak, travel, msg}
"""
import json, os, sys, urllib.request, urllib.error, random, datetime, base64

CHECKIN_HOST = "https://www.codebuddy.cn"
TRAVEL_HOST = "https://www.workbuddy.cn"
# 飞书群机器人 webhook 从环境变量 / GitHub Secret 读取，避免仓库公开后泄露
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "").strip()

# uid 前缀 -> 昵称（用于推送消息可读性，来源：WorkDaddy /api/accounts 权威数据）
NAMES = {
    "3e9b8d9c": "账号1",
    "a33f13d9": "祥",
    "ef74e8b5": "蹦蹦跳跳",
}

def acc_name(uid):
    return NAMES.get(uid[:8], uid[:8])

def token_info(token):
    """解析 accessToken 过期信息：JWT 直接读 exp 得出剩余天数；其他类型无法预测。"""
    try:
        parts = token.split(".")
        if len(parts) == 3:
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            exp = payload.get("exp")
            if exp:
                now = datetime.datetime.now(datetime.timezone.utc).timestamp()
                left = (float(exp) - now) / 86400.0
                exp_bjt = datetime.datetime.fromtimestamp(float(exp), datetime.timezone(datetime.timedelta(hours=8)))
                return {"type": "jwt", "exp": exp_bjt.strftime("%Y-%m-%d %H:%M"), "days_left": round(left, 1)}
            return {"type": "jwt-noexp"}
    except Exception:
        pass
    return {"type": "opaque"}

def api(url, token, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 WorkBuddy"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode("utf-8")
            try: return r.status, json.loads(txt)
            except Exception: return r.status, {"_raw": txt[:200]}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        try:
            return e.code, json.loads(body)
        except Exception:
            # 非 JSON（网关/WAF 返回 HTML 错误页，可按网关错误处理）
            return e.code, {"_raw": body[:200]}
    except Exception as e:
        return 0, {"_err": str(e)}

def is_gateway_error(status, resp):
    """识别网关/WAF 临时错误（HTTP 401 带 HTML 页等，需与令牌过期区分）。"""
    raw = str(resp.get("_raw", ""))
    if status in (429,) or status >= 500:
        return True
    if status == 401 and ("<html" in raw.lower() or "openresty" in raw.lower() or not raw.strip()):
        return True
    if status == 0:
        return True
    return False

def api_status_retry(url, token, tries=4):
    """带状态码重试：网关/WAF 临时错误指数退避（可区分令牌过期）。"""
    last = (0, {})
    for i in range(tries):
        sc, s = api(url, token, body={})
        if sc == 200:
            return sc, s
        if sc == 401 and not is_gateway_error(sc, s):
            return sc, s  # 真·令牌过期（API 返回 JSON 401)
        last = (sc, s)
        import time
        time.sleep(3 * (i + 1))
    return last

def travel_flow(token):
    st_code, st = api(TRAVEL_HOST + "/activity/growth/buddy/travel/status", token)
    if st_code != 200:
        return {"state": "unavailable", "note": "travel status HTTP %s" % st_code}
    d = st.get("data") or st
    state = d.get("state")
    info = {"state": state}
    if state == "arrived":
        c_code, c = api(TRAVEL_HOST + "/activity/growth/buddy/travel/claim", token, body={})
        if c_code == 200:
            cd = c.get("data") or {}
            info["claimed"] = cd.get("reward_credit", True)
        else:
            info["claimed"] = False  # 400=no unclaimed travel, 幂等可忽略
    if state in ("idle", "arrived"):
        if not d.get("daily_limit_reached"):
            loc = random.choice([1, 2, 3, 4])
            dp_code, dp = api(TRAVEL_HOST + "/activity/growth/buddy/travel/depart", token, body={"location_id": loc})
            if dp_code == 200:
                dd = dp.get("data") or {}
                info["departed"] = True
                info["arrive_at"] = dd.get("arrive_at") or dd.get("arriveAt")
            else:
                info["departed"] = False
                info["depart_err"] = "HTTP %s" % dp_code
    else:
        info["departed"] = False
        info["note"] = "今日派遣次数已用完"
    return info

def _count(v):
    """同客户端 toCount()：Number(v) 有限且 >0 才计入，否则按 0。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    if n != n or n in (float("inf"), float("-inf")) or n <= 0:
        return 0.0
    return n


def fetch_balance(token):
    """真实剩余积分：各资格包 CycleRemainCapacity 之和。

    与桌面端「剩余积分」完全同源（/billing/meter/get-user-resource-summary →
    parseResourceSummary → sumSummaryCapacity）。注意：checkin-activity-status 里的
    total_credits 是「累计签到所得」，不是余额，不能直接当余额用。
    返回 (dict|None, 错误说明)。
    """
    sc, r = api(CHECKIN_HOST + "/billing/meter/get-user-resource-summary", token, body={})
    if sc != 200 or not isinstance(r.get("data"), dict):
        return None, "HTTP %s" % sc
    packs = [p for p in (r["data"].get("Packages") or []) if isinstance(p, dict)]
    total = sum(_count(p.get("CycleTotalCapacity")) for p in packs)
    left = sum(_count(p.get("CycleRemainCapacity")) for p in packs)
    return {"left": round(left, 2), "total": round(total, 2), "packages": len(packs)}, ""


def mask_phone(p):
    """手机号脱敏：138****1234。"""
    d = "".join(ch for ch in str(p or "") if ch.isdigit())
    if len(d) >= 7:
        return d[:3] + "****" + d[-4:]
    return str(p or "")


def fetch_profile(token, uid):
    """真实账号名：GET /console/accounts（客户端同源接口）→ nickname / phoneNumber。
    返回 {} 表示取不到，调用方回退到内置昵称表。"""
    sc, r = api(CHECKIN_HOST + "/console/accounts", token)
    if sc != 200:
        return {}
    data = r.get("data") if isinstance(r, dict) else None
    accs = (data or {}).get("accounts") if isinstance(data, dict) else None
    if not isinstance(accs, list) or not accs:
        return {}
    accs = [a for a in accs if isinstance(a, dict)]
    pick = next((a for a in accs if str(a.get("uid") or "") == str(uid)), None)
    if pick is None:
        pick = next((a for a in accs if str(a.get("uid") or "").startswith(str(uid)[:8])), None)
    if pick is None and len(accs) == 1:
        pick = accs[0]
    if not pick:
        return {}
    return {"nickname": str(pick.get("nickname") or "").strip(),
            "phone": mask_phone(pick.get("phoneNumber") or pick.get("phone_number")),
            "account_type": pick.get("type") or ""}


def run_account(acc):
    uid, token = acc.get("uid", "?"), acc.get("accessToken", "")
    out = {"uid": uid, "status": "error", "msg": ""}
    out["token"] = token_info(token)
    if not token:
        out["msg"] = "缺少 accessToken"
        return out
    # 真实账号名 / 手机号（取不到就沿用内置昵称表）
    try:
        prof = fetch_profile(token, uid)
    except Exception:
        prof = {}
    if prof:
        out["nickname"] = prof.get("nickname")
        out["phone"] = prof.get("phone")
    sc, s = api_status_retry(CHECKIN_HOST + "/billing/meter/checkin-activity-status", token)
    if sc != 200 or not isinstance(s.get("data"), dict):
        if sc == 401 and not is_gateway_error(sc, s):
            out["msg"] = "令牌无效或已过期(HTTP 401)，需重新提取登录态"
        elif is_gateway_error(sc, s):
            out["msg"] = "网关临时错误(HTTP %s)，已重试仍失败，建议 2 小时后重跑" % sc
        else:
            out["msg"] = "签到状态查询失败 HTTP %s %s" % (sc, str(s)[:80])
        return out
    sd = s["data"]
    # 状态接口若返回过期类字段，记录到结果（日志诊断用，不含敏感值）
    for k, v in sd.items():
        if any(w in k.lower() for w in ("expire", "expiry", "deadline")):
            out.setdefault("expire_fields", {})[k] = v
    out["streak"] = sd.get("streak_days")
    # 注意：这是「累计签到所得」，不是余额。真实余额走 get-user-resource-summary，见文末。
    out["cumulative_credits"] = sd.get("total_credits")
    if sd.get("today_checked_in"):
        out["status"] = "ok"
        out["action"] = "skip_already_signed"
        out["points"] = 0
    else:
        cc, c = api(CHECKIN_HOST + "/billing/meter/daily-checkin", token, body={})
        cd = c.get("data") or {}
        if cc == 200 and c.get("code") == 0:
            out["status"] = "ok"
            out["action"] = "checked_in"
            out["points"] = cd.get("daily_credit", sd.get("daily_credit"))
        elif cc == 200 and c.get("code") == 10001:
            out["status"] = "ok"
            out["action"] = "skip_already_signed"
            out["points"] = 0
        else:
            out["msg"] = "签到失败 HTTP %s code=%s msg=%s" % (cc, c.get("code"), c.get("msg"))
            return out
    try:
        out["travel"] = travel_flow(token)
    except Exception as e:
        out["travel"] = {"state": "error", "note": str(e)[:100]}
    # 余额放在最后取：签到时点和旅行奖励都已结算，拿到的是真实剩余
    bal, berr = fetch_balance(token)
    if bal:
        out["balance"] = bal["left"]
        out["balance_total"] = bal["total"]
        out["balance_packages"] = bal["packages"]
    else:
        out["balance"] = sd.get("total_credits")
        out["balance_note"] = "真实余额查询失败(%s)，暂回退为累计签到积分" % berr
    return out

def display_name(r):
    """优先用接口取到的真实昵称，其次内置昵称表，附脱敏手机号。"""
    n = (r.get("nickname") or "").strip() or acc_name(r.get("uid", "?"))
    return "%s（%s）" % (n, r["phone"]) if r.get("phone") else n


def feishu_push(results, force=False):
    """把签到汇总推送到飞书群机器人，返回 (ok, note)。"""
    if not FEISHU_WEBHOOK:
        print("skip push: FEISHU_WEBHOOK not configured")
        return False, "skipped (no FEISHU_WEBHOOK)"
    # 令牌过期预警：剩余不足 2 天（即明天或今天到期）时提醒
    warns = []
    for r in results:
        t = r.get("token") or {}
        if t.get("days_left") is not None and t["days_left"] < 2:
            warns.append("%s 的登录令牌将于 %s 过期（剩 %.1f 天）" % (
                display_name(r), t.get("exp"), t["days_left"]))
    # 全部账号均为「今日已签」且无异常、无预警时跳过推送，避免 GitHub schedule 兜底运行造成重复推送
    # force=True 时跳过「今日已签不重复推送」的判断，用于手动自测
    if results and not force and not warns and all(r.get("status") == "ok" and r.get("action") != "checked_in" for r in results):
        print("skip push: all accounts already checked in today")
        return False, "skipped (all already checked in)"
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %A")
    lines = ["📋 WorkBuddy 每日签到报告", "🗓 %s" % today, "——————————————"]
    errs = []
    for i, r in enumerate(results, 1):
        name = display_name(r)
        if r.get("status") == "ok":
            act = r.get("action")
            if act == "checked_in":
                mark, acttxt = "✅", "签到成功 +{}积分".format(r.get("points", 0))
            else:
                mark, acttxt = "☑️", "今日已签，跳过"
            tv = r.get("travel") or {}
            tstate = tv.get("state")
            if tstate == "traveling":
                ttxt = "🐱旅行中"
            elif tv.get("departed"):
                ttxt = "🐱已派出"
            elif tv.get("claimed"):
                ttxt = "🐱旅行积分已领"
            elif tstate == "idle":
                ttxt = "🐱待出发"
            else:
                ttxt = "🐱%s" % (tstate or "未知")
            cum = r.get("cumulative_credits")
            lines.append("%d. %s：%s（连续%s天）｜累计签到 %s｜剩余 %s%s｜%s" % (
                i, name, acttxt, r.get("streak", "?"),
                cum if cum is not None else "?",
                r.get("balance", "?"),
                "（累计值·真实余额查询失败）" if r.get("balance_note") else "", ttxt))
        else:
            msg = r.get("msg") or "未知错误"
            lines.append("%d. %s：❌ %s" % (i, name, msg))
            errs.append(name)
    lines.append("——————————————")
    if warns:
        lines.append("⏰ %s，请尽快重新提取 accessToken 并更新 GitHub Secret！" % "；".join(warns))
    if errs:
        lines.append("⚠️ 异常账号：%s，请关注！" % "、".join(errs))
    else:
        lines.append("🎉 全部账号签到流程执行完毕")
    payload = json.dumps({"msg_type": "text", "content": {"text": "\n".join(lines)}}).encode()
    req = urllib.request.Request(FEISHU_WEBHOOK, data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            ok = resp.status == 200 and json.loads(body).get("code") == 0
            return ok, body[:120]
    except Exception as e:
        return False, str(e)[:120]

def main():
    args = sys.argv[1:]
    if not args:
        print("用法: python wb_daily.py <tokens.json> [--no-push] [--force-push]")
        return 2
    accs = json.load(open(args[0], encoding="utf-8"))
    results = [run_account(a) for a in accs]
    print(json.dumps(results, ensure_ascii=False, indent=1))
    # 推送飞书（推送失败不影响签到结果本身）
    if "--no-push" in args:
        push = {"ok": False, "note": "skipped (--no-push)"}
    else:
        ok, note = feishu_push(results, force="--force-push" in args)
        push = {"ok": ok, "note": note}
    print(json.dumps({"feishu_push": push}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    main()
