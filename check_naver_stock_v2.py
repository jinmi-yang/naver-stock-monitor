"""
네이버 브랜드스토어 재입고 체크 — v4
====================================
변경점:
- 실제 JSON 응답 분석을 바탕으로 `soldout` boolean 을 1차 신호로 사용
  (구매하기 버튼 활성화 여부와 1:1 매칭)
- HTML 체크는 제거 (페이지가 JS 렌더링이라 requests로 안 보임)
- statusType 으로 2차 검증
- 옵션 재고 현황도 로그에 출력 (참고용)
"""
import os
import re
import sys
import ssl
import json
import time
import random
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

import requests


PRODUCT_URL = os.environ.get(
    "PRODUCT_URL",
    "https://m.brand.naver.com/toocoolforschool/products/13446961852",
)
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
EMAIL_TO   = os.environ.get("EMAIL_TO", "") or EMAIL_USER

STATE_FILE = Path("state.json")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; SM-S918N) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })
    return s


def parse_url(url):
    m = re.search(r"brand\.naver\.com/([^/]+)/products/(\d+)", url)
    if not m:
        raise ValueError(f"URL 파싱 실패: {url}")
    return m.group(1), m.group(2)


def fetch_channel_uid(s, brand, pid):
    """페이지를 1회 방문해 channelUid 추출 + 쿠키 확보"""
    url = f"https://m.brand.naver.com/{brand}/products/{pid}"
    r = s.get(url, timeout=20, headers={
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "*/*;q=0.8"),
        "Upgrade-Insecure-Requests": "1",
    })
    r.raise_for_status()
    for pat in (r'"channelUid"\s*:\s*"([^"]+)"',
                r"channelUid['\"]?\s*[:=]\s*['\"]([^'\"]+)"):
        m = re.search(pat, r.text)
        if m:
            return m.group(1)
    raise RuntimeError("channelUid 추출 실패")


def check_api(s, brand, pid, uid):
    url = (f"https://brand.naver.com/n/v2/channels/{uid}"
           f"/products/{pid}?withWindow=false")
    r = s.get(url, timeout=20, headers={
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://m.brand.naver.com/{brand}/products/{pid}",
    })
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"차단 의심 HTTP {r.status_code}")
    r.raise_for_status()
    return r.json()


def notify_email(subject: str, body: str):
    if not EMAIL_USER or not EMAIL_PASS:
        print("[이메일 미발송] EMAIL_USER/EMAIL_PASS 미설정")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr(("Naver Stock Bot", EMAIL_USER))
        msg["To"] = EMAIL_TO
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as srv:
            srv.login(EMAIL_USER, EMAIL_PASS)
            srv.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
        print(f"이메일 발송 완료 → {EMAIL_TO}")
    except Exception as e:
        print(f"이메일 발송 실패: {e}")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"notified": False}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def summarize_options(options):
    """옵션 재고 요약 (참고용)"""
    if not options:
        return "(옵션 없음)"
    in_stock = [(o.get("optionName1", ""), o.get("stockQuantity", 0))
                for o in options if o.get("stockQuantity", 0) > 0]
    if not in_stock:
        return f"전체 {len(options)}개 옵션 모두 재고 0"
    parts = [f"{name}({qty})" for name, qty in in_stock[:5]]
    return f"{len(in_stock)}/{len(options)}개 옵션 재고 있음: " + ", ".join(parts)


def main():
    brand, pid = parse_url(PRODUCT_URL)
    state = load_state()

    delay = random.uniform(0, 45)
    print(f"시작 지연 {delay:.1f}초...")
    time.sleep(delay)

    s = make_session()

    try:
        uid = fetch_channel_uid(s, brand, pid)
        print(f"channelUid: {uid}")
        time.sleep(random.uniform(2, 5))
        data = check_api(s, brand, pid, uid)
    except Exception as e:
        print(f"[오류] {type(e).__name__}: {e}")
        return

    # 핵심 신호: soldout boolean (구매하기 버튼 활성화 여부와 매칭)
    soldout = data.get("soldout")
    status_type = data.get("statusType") or data.get("productStatusType")
    stock_total = data.get("stockQuantity")
    name = data.get("dispName") or data.get("name") or "(이름없음)"
    price = data.get("discountedSalePrice") or data.get("salePrice")
    options = data.get("optionCombinations") or data.get("options") or []
    options_summary = summarize_options(options)

    print(f"[상품] {name}")
    print(f"[신호] soldout={soldout}, statusType={status_type}, "
          f"총재고={stock_total}, 가격={price}")
    print(f"[옵션] {options_summary}")

    # 판매 가능 = soldout이 명시적으로 False 이고 statusType이 OUTOFSTOCK이 아닐 것
    available = (soldout is False) or (status_type != "OUTOFSTOCK")
    print(f"[판정] 구매가능={available}")

    if available and not state.get("notified", False):
        subject = "네이버 재입고 알림"
        body = (f"{name}\n"
                f"가격: {price}원\n"
                f"옵션: {options_summary}\n\n"
                f"{PRODUCT_URL}")
        notify_email(subject, body)
        state["notified"] = True
    elif not available:
        state["notified"] = False  # 다음 재입고 시 알림 재개

    state["soldout"] = soldout
    state["statusType"] = status_type
    state["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


if __name__ == "__main__":
    main()
