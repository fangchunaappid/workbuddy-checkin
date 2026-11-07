# -*- coding: utf-8 -*-
"""
weibo_backfill.py - 一次性补抓微博监控漏推的帖子并推送到飞书

背景: 2026-11-06 20:25 ~ 11-07 16:29 期间, m.weibo.cn 对匿名访客加了翻页登录墙,
      监控脚本在"第2页起"失败, 导致博主在断档窗口内的帖子没有推送。
      本脚本用你的登录 Cookie(SUB / SUBP) 在本机翻页, 把漏掉的帖子补推到飞书。

用法(在本机运行, 需要 Python3, 无第三方依赖):
    python3 weibo_backfill.py --sub "你的SUB" --subp "你的SUBP" \
        --webhook "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx" \
        [--secret "飞书签名密钥(没有可不填)"] [--dry-run] [--max-posts 200]

说明:
    --sub / --subp   必填, m.weibo.cn 登录态的 Cookie 值
    --webhook        必填, 飞书机器人 webhook (仓库 Settings -> Secrets -> FEISHU_WEBHOOK)
    --secret         可选, 如果飞书机器人开了"签名校验"才填
    --dry-run        只统计漏贴数量和预览, 不实际推送
    --max-posts      最多推送条数(缺省 200), 防止一次性刷屏
"""
import argparse
import base64
import hashlib
import hmac
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")
MAX_BODY_LEN = 1800
TZ8 = timezone(timedelta(hours=8))

# ---- 补抓范围(已按仓库实际断档确定, 一般不用改) ----
UID = "3962719063"
SCREEN_NAME = "挖地瓜的超级鹿鼎公"
START_ID = "5362352524361996"   # 断档前的基线 last_id(不含), 只补比它新的
STOP_ID = "5362655578819639"    # 已经推送过的最老一条(不含), 避免重复

MAX_PAGES = 60  # 最多翻多少页(每页约10条)


def _api_headers(cookie):
    return {
        "User-Agent": UA,
        "Referer": "https://m.weibo.cn/",
        "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
    }


def fetch_posts(uid, cookie, page):
    url = (f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}"
           f"&containerid=107603{uid}&page={page}")
    req = urllib.request.Request(url, headers=_api_headers(cookie))
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("ok") == 1:
        cards = (data.get("data") or {}).get("cards") or []
        return [c["mblog"] for c in cards if c.get("mblog")]
    if data.get("ok") == -100:
        raise RuntimeError(
            "Cookie 未被微博接受(ok=-100, 要求登录)。多为使用云端/异地网络导致的风控; "
            "请务必在你本人日常使用的网络(家用网络)下运行本脚本。")
    raise RuntimeError(f"接口返回 ok={data.get('ok')} msg={str(data)[:150]}")


def clean_text(raw):
    t = re.sub(r"<br\s*/?>", "\n", raw or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def fmt_time(s):
    if not s:
        return ""
    s = str(s).strip()
    if not re.match(r"^[A-Z][a-z]{2} ", s):
        return s
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(TZ8).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s


def fetch_full_text(pid, cookie):
    url = f"https://m.weibo.cn/statuses/show?id={pid}"
    try:
        req = urllib.request.Request(url, headers=_api_headers(cookie))
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok") == 1:
            return clean_text((data.get("data") or {}).get("text"))
    except Exception:
        pass
    return None


def post_summary(mb, cookie):
    pid = str(mb.get("id") or mb.get("mid") or "")
    text = clean_text(mb.get("text"))
    if mb.get("isLongText"):
        full = fetch_full_text(pid, cookie)
        if full:
            text = full
    text = re.sub(r"(?:…|⋯|\.{2,}|。{2,})?\s*全文$", "", text).strip()
    ret = mb.get("retweeted_status")
    if ret:
        ret_user = (ret.get("user") or {}).get("screen_name", "")
        ret_text = clean_text(ret.get("text"))[:300]
        ret_text = re.sub(r"(?:…|⋯|\.{2,}|。{2,})?\s*全文$", "", ret_text).strip()
        text = f"{text}\n\n———— 转发 @{ret_user}: {ret_text}".strip()
    pics = []
    for p in (mb.get("pics") or [])[:9]:
        url = (p.get("large") or p).get("url")
        if url:
            pics.append(url)
    if pics:
        text += "\n\n" + " ".join(f"[图{i + 1}]({u})" for i, u in enumerate(pics))
    bid = mb.get("bid")
    if not bid:
        # 缺 bid 时从单条接口拿
        try:
            req = urllib.request.Request(
                f"https://m.weibo.cn/statuses/show?id={pid}", headers=_api_headers(cookie))
            with urllib.request.urlopen(req, timeout=20) as resp:
                bid = (json.loads(resp.read().decode("utf-8")).get("data") or {}).get("bid")
        except Exception:
            bid = ""
    link = f"https://weibo.com/{UID}/{bid}" if bid else f"https://m.weibo.cn/detail/{pid}"
    if len(text) > MAX_BODY_LEN:
        text = text[:MAX_BODY_LEN] + " ..."
    text += f"\n\n[原微博]({link})"
    return int(pid), fmt_time(mb.get("created_at")), text


def push_feishu(webhook, secret, time_str, body):
    header = {"template": "blue",
              "title": {"tag": "plain_text", "content": f"📢 {SCREEN_NAME} 更新了微博"}}
    elements = [{"tag": "div", "text": {"tag": "lark_md",
                                        "content": f"**{time_str}**\n{body}"}}]
    card = {"config": {"wide_screen_mode": True}, "header": header, "elements": elements}
    payload = {"msg_type": "interactive", "card": card}
    if secret:
        ts = str(int(time.time()))
        sign = base64.b64encode(
            hmac.new(f"{ts}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
        ).decode()
        payload["timestamp"] = ts
        payload["sign"] = sign
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") not in (0, None) and result.get("StatusCode") != 0:
        raise RuntimeError(f"飞书推送失败: {result}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", required=True)
    ap.add_argument("--subp", required=True)
    ap.add_argument("--webhook", required=True)
    ap.add_argument("--secret", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-posts", type=int, default=200)
    args = ap.parse_args()

    cookie = f"SUB={args.sub}; SUBP={args.subp}"
    start = int(START_ID)
    stop = int(STOP_ID)

    print(f"补抓范围: {START_ID} < id < {STOP_ID}", flush=True)

    collected = []  # (id, time_str, text)
    for page in range(1, MAX_PAGES + 1):
        try:
            posts = fetch_posts(UID, cookie, page)
        except RuntimeError as e:
            if page == 1:
                print(f"✗ {e}", file=sys.stderr)
                sys.exit(2)
            break
        if not posts:
            break
        page_gap = [mb for mb in posts
                    if start < int(str(mb.get("id") or mb.get("mid") or 0)) < stop]
        collected.extend((int(mb.get("id")), None, mb) for mb in page_gap)
        # 已翻到 <= 基线 的帖子, 说明断档区间全部找到, 停止
        oldest = min((int(str(mb.get("id") or mb.get("mid") or 0)) for mb in posts), default=0)
        if oldest <= start:
            break
        time.sleep(1)

    # 按时间从旧到新排序
    collected.sort(key=lambda x: x[0])
    total = len(collected)

    print(f"共发现 {total} 条漏推帖子", flush=True)
    if total == 0:
        print("没有需要补推的帖子。")
        return

    if args.dry_run:
        for pid, _, mb in collected[:10]:
            _, t, body = post_summary(mb, cookie)
            print(f"\n[{pid}] {t}\n{body[:120]}...")
        if total > 10:
            print(f"\n... 其余 {total - 10} 条省略(dry-run)")
        print("\n(dry-run, 未实际推送)")
        return

    if total > args.max_posts:
        print(f"⚠ 漏推数量 {total} 超过 --max-posts={args.max_posts}, 只推最旧的 {args.max_posts} 条")
        collected = collected[:args.max_posts]

    ok = fail = 0
    for i, (pid, _, mb) in enumerate(collected, 1):
        _, t, body = post_summary(mb, cookie)
        try:
            push_feishu(args.webhook, args.secret, t, body)
            ok += 1
            print(f"[{i}/{len(collected)}] 已推送 {pid}", flush=True)
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(collected)}] 推送失败 {pid}: {e}", file=sys.stderr, flush=True)
        time.sleep(2)  # 飞书机器人频率控制
    print(f"\n完成: 成功 {ok} 条, 失败 {fail} 条。")


if __name__ == "__main__":
    main()