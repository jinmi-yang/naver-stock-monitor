"""
네이버 브랜드스토어 재입고 체크 — GitHub Actions / Cron 단발 실행
================================================================
알림 방식:
  1) 이메일 (Gmail SMTP) — 추천
  2) 카카오톡 "나에게 보내기" — 선택 (토큰 관리 필요)

환경변수 (필요한 것만 채우면 됨):
  PRODUCT_URL    체크할 상품 URL

  # 이메일
  EMAIL_USER     Gmail 주소 (예: me@gmail.com)
  EMAIL_PASS     Gmail 앱 비밀번호 16자리 (공백 없이)
  EMAIL_TO       받는 사람 이메일 (생략시 EMAIL_USER 와 동일)

  # 카카오 (선택)
  KAKAO_REST_KEY      카카오 앱 REST API 키
  KAKAO_REFRESH_TOKEN 카카오 리프레시 토큰
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

# 이메일 설정
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
EMAIL_TO   = os.environ.get("EMAIL_TO", "") or EMAIL_USER

# 카카오 설정
KAKAO_REST_KEY      = os.environ.get("KAKAO_REST_KEY", "")
KAKAO_REFRESH_TOKEN = os.environ.get("KAKAO_REFRESH_TOKEN", "")

STATE_FILE = Path("state.json")


# ==============================================================
# 세션
# ==============================================================
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
    raise RuntimeError("channelUid 추출 실패 (차단 또는 페이지 변경 의심)")


def check_status(s, brand, pid, uid):
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


# ==============================================================
# 알림: 이메일 (Gmail SMTP)
# ==============================================================
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
        print(f"✅ 이메일 발송 완료 → {EMAIL_TO}")
    except Exception as e:
        print(f"⚠️ 이메일 발송 실패: {e}")


# ==============================================================
# 알림: 카카오톡 "나에게 보내기"
# ==============================================================
def kakao_refresh_access_token():
    """리프레시 토큰으로 새 액세스 토큰 발급"""
    r = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": KAKAO_REST_KEY,
            "refresh_token": KAKAO_REFRESH_TOKEN,
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()  # access_token, (refresh_token), expires_in 등


def notify_kakao(text: str, link_url: str):
    if not KAKAO_REST_KEY or not KAKAO_REFRESH_TOKEN:
        print("[카카오 미발송] KAKAO_REST_KEY/REFRESH_TOKEN 미설정")
        return
    try:
        tokens = kakao_refresh_access_token()
        access_token = tokens["access_token"]

        # 리프레시 토큰이 갱신되어 응답에 같이 오면 사용자가 갱신해야 함
        if "refresh_token" in tokens:
            print("⚠️ 카카오 리프레시 토큰이 갱신되었습니다. Secret 업데이트 필요:")
            print(f"   새 KAKAO_REFRESH_TOKEN={tokens['refresh_token']}")

        # 텍스트 템플릿
        template = {
            "object_type": "text",
            "text": text[:200],  # 200자 제한
            "link": {"web_url": link_url, "mobile_web_url": link_url},
            "button_title": "상품 보러가기",
        }
        r = requests.post(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"template_object": json.dumps(template, ensure_ascii=False)},
            timeout=10,
        )
        r.raise_for_status()
        result = r.json()
        if result.get("result_code") == 0:
            print("✅ 카카오톡 발송 완료")
        else:
            print(f"⚠️ 카카오톡 응답: {result}")
    except Exception as e:
        print(f"⚠️ 카카오톡 발송 실패: {e}")


# ==============================================================
# 상태 저장
# ==============================================================
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"notified": False, "last_status": None}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ==============================================================
# 메인
# ==============================================================
def main():
    brand, pid = parse_url(PRODUCT_URL)
    state = load_state()

    delay = random.uniform(0, 45)
    print(f"시작 지연 {delay:.1f}초...")
    time.sleep(delay)

    s = make_session()
    try:
        uid = fetch_channel_uid(s, brand, pid)
        time.sleep(random.uniform(2, 5))
        data = check_status(s, brand, pid, uid)
    except Exception as e:
        print(f"[오류] {type(e).__name__}: {e}")
        return

    stype = data.get("statusType") or data.get("productStatusType")
    stock = data.get("stockQuantity")
    name = data.get("dispName") or data.get("name") or "(이름없음)"
    price = data.get("discountedSalePrice") or data.get("salePrice")
    print(f"[{name}] status={stype}, stock={stock}, price={price}")

    available = (
        stype == "SALE"
        and isinstance(stock, int)
        and stock > 0
    )

    if available and not state.get("notified", False):
        subject = "🎉 네이버 재입고 알림"
        body = (f"{name}\n"
                f"재고 {stock}개 · {price}원\n\n"
                f"{PRODUCT_URL}")

        notify_email(subject, body)
        notify_kakao(f"{subject}\n{body}", PRODUCT_URL)

        state["notified"] = True
    elif not available:
        state["notified"] = False

    state["last_status"] = stype
    state["last_stock"] = stock
    state["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


if __name__ == "__main__":
    main()
