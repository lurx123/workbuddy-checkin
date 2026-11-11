#!/usr/bin/env python3
"""WorkBuddy 云端猫猫旅行（GitHub Actions 版，零第三方依赖）。

为什么放云端：猫猫一天只能派 1 趟、到站领奖 +5~10 积分，是本机不开机时
唯一还能拿到的积分来源。接口与签到同域同鉴权，没有任何设备绑定 ——
实测用 checkin_one.py 那套极简 header 直接 GET 就返回 code=0
（X-Request-ID / X-Machine-ID / X-Session-ID 都不需要）。

接口（实测，别改）：
  GET  /v2/activity/growth/buddy/travel/config   地点清单（时长/奖励区间）
  GET  /v2/activity/growth/buddy/travel/status   state: idle / traveling / arrived
                                                 daily_limit_reached / arrive_at / server_now
  GET  /v2/activity/growth/buddy/travel/records  历史，含 claimed_at（用于补领）
  POST /v2/activity/growth/buddy/travel/depart   body {"location_id": N}
  POST /v2/activity/growth/buddy/travel/claim    body {}

幂等设计：
  · 一天只能派 1 趟，所以 depart 只在「本次运行开始时 state=idle 且当日没用完」才发
  · 到站没领的记录下次运行会补领（最近几条里找 arrive_at 已过且 claimed_at 为空）
  · 写操作只打一次、绝不重试：响应丢了就交给下一次 cron，6 条 cron 兜底

环境变量: WB_TOKEN / WB_REFRESH / WB_UID / WB_REALM
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

HOST = {"cn": "https://www.workbuddy.cn", "intl": "https://www.workbuddy.ai"}
CHAT = {"cn": "https://copilot.tencent.com", "intl": "https://www.workbuddy.ai"}
UA = {
    "cn": "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1",
    "intl": "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2",
}
TRAVEL = "/v2/activity/growth/buddy/travel"
CATCHUP_WINDOW = 3 * 86400   # 补领只看最近 3 天，避免老记录天天刷失败


def log(m):
    print(m, flush=True)


def hdr(realm, uid, tok):
    base = HOST[realm]
    h = {
        "Authorization": "Bearer " + tok,
        "X-User-Id": uid,
        "X-Domain": CHAT[realm],
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": UA[realm],
        "Origin": base,
        "Referer": base + "/",
    }
    if realm == "cn":
        h["X-Product"] = "SaaS"
    return h


def call(url, h, method="POST", body=None, timeout=30):
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"msg": raw[:200]}
    except Exception as e:
        return 0, {"msg": str(e)[:200]}


def do_refresh(realm, uid, rt):
    url = CHAT[realm] + "/v2/auth/token/refresh"
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": UA[realm],
        "Origin": CHAT[realm],
        "Referer": CHAT[realm] + "/",
        "X-Refresh-Token": rt,
        "X-Auth-Refresh-Source": "workbuddy" if realm == "cn" else "plugin",
        "X-User-Id": uid,
        "X-Domain": CHAT[realm],
    }
    st, js = call(url, h, body=b"{}")
    if st != 200:
        log("  refresh HTTP %s: %s" % (st, str(js)[:150]))
        return None
    d = js.get("data") or {}
    d = d.get("data") or d
    tok = d.get("accessToken")
    if not tok:
        log("  refresh 无 token: %s" % str(js)[:150])
        return None
    log("  refresh 成功")
    return tok, d.get("refreshToken") or rt


def data_of(js):
    return js.get("data") or {}


def get(realm, uid, tok, path):
    """只读：两个基址都试，返回 (status, json)。"""
    last = (0, {})
    for base in (CHAT[realm], HOST[realm]):
        st, js = call(base + path, hdr(realm, uid, tok), method="GET")
        if st == 200 and js.get("code") == 0:
            return st, js
        last = (st, js)
    return last


def post_once(realm, uid, tok, path, body):
    """写操作：只打一次、绝不重试。"""
    return call(CHAT[realm] + path, hdr(realm, uid, tok),
                method="POST", body=json.dumps(body).encode())


def pick_location(realm, uid, tok):
    """挑时长最短的地点（四个地点时长/奖励区间完全一样，纯属固定选择）。"""
    st, js = get(realm, uid, tok, TRAVEL + "/config")
    locs = data_of(js).get("locations") or []
    if not locs:
        return 1
    locs = sorted(locs, key=lambda x: (x.get("duration_hours_min") or 99, x.get("id") or 99))
    return locs[0].get("id") or 1


def try_claim(realm, uid, tok):
    st, js = post_once(realm, uid, tok, TRAVEL + "/claim", {})
    if st == 200 and js.get("code") == 0:
        d = data_of(js)
        credit = d.get("reward_credit") or d.get("credit") or 0
        log("::notice::猫猫领奖成功 +%s 积分" % credit)
        return True, credit
    log("::warning::猫猫领奖未成功 HTTP %s code=%s %s"
        % (st, js.get("code"), str(js.get("msg"))[:80]))
    return False, 0


def find_unclaimed(realm, uid, tok, now):
    st, js = get(realm, uid, tok, TRAVEL + "/records?page=1&page_size=5")
    for r in data_of(js).get("records") or []:
        if r.get("claimed_at"):
            continue
        arr = r.get("arrive_at") or 0
        if arr and arr <= now and arr > now - CATCHUP_WINDOW:
            return r
    return None


def _summary(uid, text):
    sm = os.environ.get("GITHUB_STEP_SUMMARY")
    if sm:
        try:
            with open(sm, "a", encoding="utf-8") as f:
                f.write("### 猫猫 `%s`\n\n%s\n\n" % (uid, text))
        except Exception:
            pass
    log("本轮结果: %s" % text)


def main():
    tok = (os.environ.get("WB_TOKEN") or "").strip()
    rt = (os.environ.get("WB_REFRESH") or "").strip()
    uid = (os.environ.get("WB_UID") or "").strip()
    realm = (os.environ.get("WB_REALM") or "cn").strip()

    if not uid:
        log("::error::缺少 WB_UID")
        return 2
    if realm != "cn":
        log("::notice::国际站没有猫猫旅行，跳过")
        return 0
    if not tok and not rt:
        log("::error::WB_TOKEN 与 WB_REFRESH 均为空")
        return 2

    log("账号 %s  realm=%s" % (uid, realm))

    if not tok:
        got = do_refresh(realm, uid, rt)
        if not got:
            log("::error::无 accessToken 且续期失败")
            return 1
        tok, rt = got

    st, js = get(realm, uid, tok, TRAVEL + "/status")
    if st in (401, 403) and rt:
        got = do_refresh(realm, uid, rt)
        if got:
            tok, rt = got
            st, js = get(realm, uid, tok, TRAVEL + "/status")
    if st in (401, 403):
        log("::error::令牌失效，且无法续期")
        return 1
    if st != 200 or js.get("code") != 0:
        log("::error::状态查询失败 HTTP %s %s" % (st, str(js)[:150]))
        return 1

    d = data_of(js)
    state = d.get("state")
    limit = d.get("daily_limit_reached")
    now = d.get("server_now") or int(time.time())
    loc = (d.get("location") or {}).get("name")
    log("状态=%s 地点=%s 当日已派完=%s" % (state, loc, limit))

    done = []

    # ---- 已在路上：没到站就什么都不做 ----
    if state == "traveling":
        left = max(0, (d.get("arrive_at") or 0) - now)
        if left > 0:
            h, m = left // 3600, left % 3600 // 60
            log("::notice::猫猫在路上（%s），还有 %dh%dm 到站，本次不动" % (loc, h, m))
            _summary(uid, "在路上（%s），%dh%dm 后到站" % (loc, h, m))
            return 0
        log("已过到站时间（%s），尝试领奖" % loc)
        ok, credit = try_claim(realm, uid, tok)
        if ok:
            done.append("领奖 +%s" % credit)
        _summary(uid, "；".join(done) or "到站但领奖未成功")
        return 0

    # ---- 已到站：领奖 ----
    if state == "arrived":
        ok, credit = try_claim(realm, uid, tok)
        if ok:
            done.append("领奖 +%s" % credit)
        _summary(uid, "；".join(done) or "到站但领奖未成功")
        return 0

    if state != "idle":
        log("::warning::未知状态 %s，本次不动" % state)
        _summary(uid, "未知状态 %s" % state)
        return 0

    # ---- 空闲：先补领历史漏领，再决定要不要派 ----
    rec = find_unclaimed(realm, uid, tok, now)
    if rec:
        log("发现未领记录 %s，尝试补领" % rec.get("record_id"))
        ok, credit = try_claim(realm, uid, tok)
        if ok:
            done.append("补领 +%s" % credit)

    if limit is True:
        log("::notice::今日已派过（daily_limit_reached=true），不派新的")
    else:
        lid = pick_location(realm, uid, tok)
        st2, js2 = post_once(realm, uid, tok, TRAVEL + "/depart", {"location_id": lid})
        if st2 == 200 and js2.get("code") == 0:
            name = (data_of(js2).get("location") or {}).get("name") or ("#%s" % lid)
            log("::notice::已派出猫猫（%s）" % name)
            done.append("派出 %s" % name)
        else:
            log("::warning::派出失败 HTTP %s code=%s %s（交给下一次 cron）"
                % (st2, js2.get("code"), str(js2.get("msg"))[:80]))

    _summary(uid, "；".join(done) or "无动作")
    return 0


if __name__ == "__main__":
    sys.exit(main())
