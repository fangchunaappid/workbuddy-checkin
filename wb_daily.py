#!/usr/bin/env python3
"""WorkBuddy 每日签到 + 派猫猫旅行（自包含精简版，标准库实现）。
用法: python wb_daily.py <tokens.json>
tokens.json: [{"uid":"...","accessToken":"..."}, ...]
输出: JSON 数组，每账号一条 {uid, status, action, points, balance, streak, travel, msg}
"""
import json, sys, urllib.request, urllib.error, random, datetime, base64

CHECKIN_HOST = "https://www.codebuddy.cn"
TRAVEL_HOST = "https://www.workbuddy.cn"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/REDACTED"

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

def run_account(acc):
    uid, token = acc.get("uid", "?"), acc.get("accessToken", "")
    out = {"uid": uid, "status": "error", "msg": ""}
    out["token"] = token_info(token)
    if not token:
        out["msg"] = "缺少 accessToken"
        return out
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
    out["balance"] = sd.get("total_credits")
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
            out["balance"] = cd.get("total_credits", out["balance"])
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
    return out

def feishu_push(results):
    """把签到汇总推送到飞书群机器人，返回 (ok, note)。"""
    # 令牌过期预警：剩余不足 2 天（即明天或今天到期）时提醒
    warns = []
    for r in results:
        t = r.get("token") or {}
        if t.get("days_left") is not None and t["days_left"] < 2:
            warns.append("%s 的登录令牌将于 %s 过期（剩 %.1f 天）" % (
                acc_name(r.get("uid", "?")), t.get("exp"), t["days_left"]))
    # 全部账号均为「今日已签」且无异常、无预警时跳过推送，避免 GitHub schedule 兜底运行造成重复推送
    if results and not warns and all(r.get("status") == "ok" and r.get("action") != "checked_in" for r in results):
        print("skip push: all accounts already checked in today")
        return False, "skipped (all already checked in)"
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %A")
    lines = ["📋 WorkBuddy 每日签到报告", "🗓 %s" % today, "——————————————"]
    errs = []
    for i, r in enumerate(results, 1):
        name = acc_name(r.get("uid", "?"))
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
            lines.append("%d. %s：%s（连续%s天）｜余额 %s｜%s" % (
                i, name, acttxt, r.get("streak", "?"), r.get("balance", "?"), ttxt))
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
    accs = json.load(open(sys.argv[1], encoding="utf-8"))
    results = [run_account(a) for a in accs]
    print(json.dumps(results, ensure_ascii=False, indent=1))
    # 推送飞书（推送失败不影响签到结果本身）
    if len(sys.argv) > 2 and sys.argv[2] == "--no-push":
        push = {"ok": False, "note": "skipped (--no-push)"}
    else:
        ok, note = feishu_push(results)
        push = {"ok": ok, "note": note}
    print(json.dumps({"feishu_push": push}, ensure_ascii=False))

if __name__ == "__main__":
    main()
