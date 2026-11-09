#!/usr/bin/env python3
"""WorkBuddy 云端签到（GitHub Actions 版，零第三方依赖）。

为什么之前的库会漏签：它的 cron 从未触发过。GitHub 对公开/私有仓库的
schedule 有已知限制：
  * 仓库连续 60 天无提交活动，定时任务会被自动停用
  * 私有仓库的 schedule 在用量高峰会延迟甚至丢弃（可能丢 30~60 分钟以上）
所以本脚本改成「一次运行内自己等到目标时刻」不可行，
改为靠 workflow 的多条 cron + 运行时的迟到容错，而不是信任单条 cron。

接口要点（都是实测，别改）：
  状态: POST /v2/billing/meter/checkin-activity-status   ← 桌面端真正用的
        （不是 checkin-status，那个返回 active=false 会误判）
  签到: POST /v2/billing/meter/daily-checkin
判定看 body 的 code：0=首签成功，10001=今天已签（HTTP 可能是 400）。

环境变量:
  WB_TOKEN / WB_REFRESH / WB_UID / WB_REALM
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


def log(m):
    print(m, flush=True)


def hdr(realm, uid, tok):
    base = HOST[realm]
    return {
        "Authorization": "Bearer " + tok,
        "X-User-Id": uid,
        "X-Domain": CHAT[realm],
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": UA[realm],
        "Origin": base,
        "Referer": base + "/",
        "X-Product": "SaaS",
    }


def call(url, h, method="POST", body=b"{}", timeout=30):
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
    st, js = call(url, h)
    if st != 200:
        log(f"  refresh HTTP {st}: {str(js)[:150]}")
        return None
    d = js.get("data") or {}
    d = d.get("data") or d
    tok = d.get("accessToken")
    if not tok:
        log(f"  refresh 无 token: {str(js)[:150]}")
        return None
    log("  refresh 成功")
    return tok, d.get("refreshToken") or rt


def status(realm, uid, tok):
    return call(HOST[realm] + "/v2/billing/meter/checkin-activity-status",
                hdr(realm, uid, tok))


def main():
    tok = (os.environ.get("WB_TOKEN") or "").strip()
    rt = (os.environ.get("WB_REFRESH") or "").strip()
    uid = (os.environ.get("WB_UID") or "").strip()
    realm = (os.environ.get("WB_REALM") or "cn").strip()

    if realm not in ("cn", "intl"):
        log(f"::error::WB_REALM 非法: {realm}")
        return 2
    if not uid:
        log("::error::缺少 WB_UID")
        return 2
    if not tok and not rt:
        log("::error::WB_TOKEN 与 WB_REFRESH 均为空")
        return 2

    log(f"账号 {uid}  realm={realm}")

    if not tok:
        got = do_refresh(realm, uid, rt)
        if not got:
            log("::error::无 accessToken 且续期失败")
            return 1
        tok, rt = got

    st, js = status(realm, uid, tok)
    code = js.get("code")
    log(f"checkin-activity-status HTTP {st} code={code}")

    if st in (401, 403) and rt:
        got = do_refresh(realm, uid, rt)
        if got:
            tok, rt = got
            st, js = status(realm, uid, tok)
            code = js.get("code")
            log(f"续期后重试 HTTP {st} code={code}")

    if st in (401, 403):
        log("::error::令牌失效，且无法续期")
        log("::notice::请在本机重跑 export_creds.py 更新 Secrets")
        return 1
    if st != 200:
        log(f"::error::状态查询失败 HTTP {st} {str(js)[:150]}")
        return 1

    d = js.get("data") or {}
    active = d.get("active")
    today = d.get("today_checked_in")
    log(f"活动={active} 今日已签={today} 连续={d.get('streak_days')} "
        f"今日积分={d.get('today_credit')}")

    if today is True:
        log("::notice::今日已签到，跳过")
        state = "already"
    else:
        st2, js2 = call(HOST[realm] + "/v2/billing/meter/daily-checkin",
                        hdr(realm, uid, tok))
        c2 = js2.get("code")
        log(f"daily-checkin HTTP {st2} code={c2} msg={js2.get('msg')}")
        if st2 in (401, 403):
            log("::error::签到鉴权失败")
            return 1
        if c2 == 0:
            d2 = js2.get("data") or {}
            log(f"::notice::签到成功 +{d2.get('credit', '-')} 积分，"
                f"连续 {d2.get('streak_days', '-')} 天")
            state = "success"
        elif c2 == 10001:
            log("::notice::今日已签到（code=10001）")
            state = "already"
        elif active is False:
            log("::warning::该账号签到活动未开启，跳过（非脚本问题）")
            state = "inactive"
        else:
            log(f"::error::签到失败 code={c2}")
            return 1

    sm = os.environ.get("GITHUB_STEP_SUMMARY")
    if sm:
        with open(sm, "a", encoding="utf-8") as f:
            f.write(f"### `{uid}`\n\n")
            f.write("| 项 | 值 |\n|---|---|\n")
            f.write(f"| realm | `{realm}` |\n")
            f.write(f"| 结果 | **{state}** |\n")
            f.write(f"| 活动开启 | {active} |\n")
            f.write(f"| 连续天数 | {d.get('streak_days')} |\n")
            f.write(f"| 今日积分 | {d.get('today_credit')} |\n")
            f.write(f"| 时间 | {time.strftime('%Y-%m-%d %H:%M:%S')} |\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
