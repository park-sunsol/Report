#!/usr/bin/env python3
"""카드사 여행 프로모션 수집기.

수집 대상: 경쟁사 8개(신한, KB국민, 롯데, 삼성, 현대, 네이버, 우리, 현대카드 PRIVIA) + 자사 1개(하나카드)
(LLM 개입 없이 requests만으로 동작)

사용법:
  python3 collect.py --diff          현재 수집 결과를 prev.json과 비교해 [변경] 블록 출력, prev.json 갱신
  python3 collect.py --diff --push   위와 동일하게 diff 계산 + notion_push_queue.json 생성
                                      (실제 Notion 반영은 이 큐를 읽는 에이전트가 수행)

환경 이슈 (Notion "수집 방법 · 환경 안내(에이전트용)" 문서 참고):
  - Playwright/Chromium 사용 불가 (프록시 TLS 실패) → requests만 사용
  - 프록시가 HEAD를 405로 거부 → 반드시 GET/POST
  - 현대카드는 legacy TLS 재협상 필요 → LegacyTLSAdapter 사용
  - 네트워크 정책이 반드시 Full이어야 함 (Trusted면 카드사 도메인 403)
"""
import argparse
import html
import json
import re
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
PREV_PATH = BASE_DIR / "prev.json"
EVENTS_PATH = BASE_DIR / "events.json"
PUSH_QUEUE_PATH = BASE_DIR / "notion_push_queue.json"

INCLUDE_KEYWORDS = [
    "항공", "숙박", "호텔", "여행사", "여행", "면세", "해외결제", "해외",
    "여행자보험", "라운지", "마일리지", "환전", "골프",
]
EXCLUDE_KEYWORDS = ["뷔페", "레스토랑", "다이닝"]
# 이 키워드가 있어도 강한 여행 키워드가 함께 있으면 제외하지 않음
STRONG_TRAVEL_KEYWORDS = ["항공", "호텔", "여행", "골프", "라운지", "마일리지", "면세"]
INSTALLMENT_KEYWORDS = ["무이자", "부분무이자", "할부"]


def is_travel_event(title: str) -> bool:
    if not any(k in title for k in INCLUDE_KEYWORDS):
        return False
    if any(k in title for k in EXCLUDE_KEYWORDS):
        return False
    if any(k in title for k in INSTALLMENT_KEYWORDS):
        if not any(k in title for k in STRONG_TRAVEL_KEYWORDS):
            return False
    return True


def clean_title(title: str) -> str:
    title = re.sub(r"<br\s*/?>", " ", title or "")
    return re.sub(r"\s+", " ", title).strip()


class LegacyTLSAdapter(HTTPAdapter):
    """현대카드는 legacy TLS 재협상이 필요 (init_poolmanager, proxy_manager_for 둘 다 오버라이드)."""

    def _ctx(self):
        ctx = ssl.create_default_context()
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().proxy_manager_for(*args, **kwargs)


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def collect_shinhan():
    events = []
    r = requests.get(
        "https://www.shinhancard.com/mob/static/json/vendor/evnPgsList01.json",
        headers={"User-Agent": UA}, timeout=15,
    )
    r.raise_for_status()
    lst = r.json()["root"]["evnlist"]
    for it in lst:
        title = clean_title(it.get("mobWbEvtNm", ""))
        if not is_travel_event(title):
            continue
        thumb = it.get("hpgEvtCtgImgUrlAr") or ""
        if thumb.startswith("/"):
            thumb = "https://www.shinhancard.com" + thumb
        link = it.get("hpgEvtDlPgeUrlAr") or ""
        if link.startswith("/"):
            link = "https://www.shinhancard.com" + link
        std, edd = it.get("mobWbEvtStd", ""), it.get("mobWbEvtEdd", "")
        period = f"{std[:4]}.{std[4:6]}.{std[6:]}~{edd[:4]}.{edd[4:6]}.{edd[6:]}" if std and edd else ""
        events.append({
            "카드사": "신한카드", "이벤트명": title, "기간": period,
            "종료일": f"{edd[:4]}-{edd[4:6]}-{edd[6:]}" if edd else "",
            "링크": link, "썸네일": thumb, "설명": "",
            "_id": it.get("mobWbEvtRvN", ""),
        })
    return events


def collect_kb():
    events = []
    url = "https://card.kbcard.com/BON/DVIEW/HBBMCXCRVNEC0001?isAjax=Y&isNoFrame=Y"
    headers = {
        "User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": "https://card.kbcard.com/BON/DVIEW/HBBMCXCRVNEC0001",
    }
    for page in range(1, 30):
        data = {
            "pageCount": str(page), "카드이벤트구분": "", "이벤트혜택구분": "ALL",
            "이벤트일련번호": "", "가맹점분류코드": "", "prevUrl": "HBBMCXCRVNEC0001",
            "대고객게시여부": "", "admin": "", "검색이벤트명": "",
        }
        r = requests.post(url, data=data, headers=headers, timeout=15)
        r.raise_for_status()
        ids = re.findall(r"goDetail\('(\d+)'", r.text)
        if not ids:
            break
        blocks = re.findall(
            r"goDetail\('(\d+)'.*?<img src=\"([^\"]*)\".*?<span class=\"subject\">(.*?)</span>\s*<span class=\"date\">([^<]*)</span>",
            r.text, re.S,
        )
        for eid, thumb, title, date in blocks:
            title = clean_title(title)
            if not is_travel_event(title):
                continue
            events.append({
                "카드사": "KB국민카드", "이벤트명": title, "기간": date.strip(),
                "종료일": _kb_end_date(date),
                "링크": f"https://card.kbcard.com/BON/DVIEW/HBBMCXCRVNEC0001?mainCC=a&eventNum={eid}",
                "썸네일": thumb, "설명": "", "_id": eid,
            })
    return events


def _kb_end_date(date_range: str) -> str:
    m = re.search(r"~\s*(\d{4})\.(\d{2})\.(\d{2})", date_range)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def collect_lotte():
    events = []
    url = "https://www.lottecard.co.kr/app/LPBNFDA_A100.lc"
    page = 1
    while True:
        r = requests.get(url, params={"pageNo": page}, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        d = r.json()
        content = d.get("Content", "")
        items = re.findall(
            r'tlfLoad\("(\d+)","load","(\d+)".*?<img src="([^"]*)"[^>]*/>\s*<span class="eventCont">\s*<b>(.*?)</b>\s*<span class="date">([^<]*)</span>',
            content, re.S,
        )
        for _, eid, thumb, title, date in items:
            title = clean_title(title)
            if not is_travel_event(title):
                continue
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            events.append({
                "카드사": "롯데카드", "이벤트명": title, "기간": date.strip(),
                "종료일": _lotte_end_date(date),
                "링크": f"https://www.lottecard.co.kr/app/LPBNFDA_V300.lc?evnBultSeq={eid}",
                "썸네일": thumb, "설명": "", "_id": eid,
            })
        param = d.get("Param", {})
        if page >= param.get("totalPage", page):
            break
        page += 1
    return events


def _lotte_end_date(date_range: str) -> str:
    m = re.search(r"~\s*(\d{4})\.(\d{2})\.(\d{2})", date_range)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def collect_samsung():
    events = []
    url = "https://www.samsungcard.com/frontservice/SHPPBE1401S02"
    pg = 0
    total = None
    while total is None or pg * 12 < total:
        r = requests.post(url, json={"pgeNo": pg}, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        d = r.json()
        total = int(d.get("totCt", 0))
        lst = d.get("listPeiHPPPrgEvnInqrDVO", [])
        if not lst:
            break
        for it in lst:
            title = clean_title(it.get("cmpTitNm", ""))
            if not is_travel_event(title):
                continue
            thumb = it.get("newThmnlImgNm") or it.get("thmnlImgNm") or ""
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            std, edd = it.get("cmsCmpStrtdt", ""), it.get("cmsCmpEnddt", "")
            period = f"{std[:4]}.{std[4:6]}.{std[6:]}~{edd[:4]}.{edd[4:6]}.{edd[6:]}" if std and edd else ""
            events.append({
                "카드사": "삼성카드", "이벤트명": title, "기간": period,
                "종료일": f"{edd[:4]}-{edd[4:6]}-{edd[6:]}" if edd else "",
                "링크": f"https://www.samsungcard.com/personal/event/ing/UHPPBE1403M0.jsp?cms_id={it.get('cmsId', '')}&cmp_id=",
                "썸네일": thumb,
                # cmpSmrCn은 삼성 전 건 공백 (통이미지) - Notion 문서 확인 사항, 채우지 않음
                "설명": "", "_id": it.get("cmpId", ""),
            })
        pg += 1
    return events


def collect_hyundai():
    events = []
    s = requests.Session()
    s.mount("https://", LegacyTLSAdapter())
    r = s.post(
        "https://www.hyundaicard.com/cpb/ev/apiCPBEV0101_05s.hc",
        json={}, headers={"User-Agent": UA}, timeout=15,
    )
    r.raise_for_status()
    lst = r.json()["bdy"]["eventList"]
    for it in lst:
        title = clean_title(it.get("bnftEvntNm", ""))
        if not is_travel_event(title):
            continue
        events.append({
            "카드사": "현대카드", "이벤트명": title,
            "기간": f"{it.get('srtDttm','')}~{it.get('endDttm','')}",
            "종료일": _hyundai_end_date(it.get("endDttm", "")),
            "링크": f"https://www.hyundaicard.com/cpb/ev/CPBEV0101_06.hc?bnftWebEvntCd={it.get('bnftWebEvntCd', '')}",
            # 이미지 CDN 베이스 경로 미확인 - 추측으로 채우지 않음
            "썸네일": "", "설명": "", "_id": it.get("bnftEvntSqno", ""),
        })
    return events


def _hyundai_end_date(dttm: str) -> str:
    m = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", dttm)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def collect_naver():
    events = []
    r = requests.get("https://travel-event.naver.com", headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    data = json.loads(m.group(1))
    sections = data.get("props", {}).get("pageProps", {}).get("hotDeals", [])
    for sec in sections:
        for p in sec.get("products", []):
            link = p.get("pcLandingUrl") or p.get("moLandingUrl") or ""
            if urlparse(link).netloc != "travel.naver.co.kr":
                # pkgtour.naver.com(패키지 상품), naver.triptopaz.com(호텔 예약) 등 타 도메인 제외
                continue
            title = clean_title(p.get("productName", ""))
            desc = " ".join(filter(None, [p.get("description1"), p.get("description2")]))
            full_title = f"{title} ({sec.get('nvTitle','')})" if sec.get("nvTitle") else title
            if not is_travel_event(full_title) and not is_travel_event(sec.get("nvTitle", "")):
                # 네이버는 트래블 전용 사이트라 섹션명(호텔/항공 등)도 함께 체크
                continue
            events.append({
                "카드사": "네이버", "이벤트명": title, "기간": "",
                "종료일": "",
                "링크": link,
                "썸네일": p.get("pcImage") or p.get("moImage") or "",
                "설명": desc, "_id": p.get("productId", ""),
            })
    return events


def collect_woori():
    events = []
    url = "https://pc.wooricard.com/dcpc/yh1/bnf/bnf02/prgevnt/getPrgEvntList.pwkjson"
    headers = {
        "User-Agent": UA, "Content-Type": "application/json;charset=UTF-8",
        "Proworks-Body": "Y", "Proworks-Lang": "ko",
        "Referer": "https://pc.wooricard.com/dcpc/yh1/bnf/bnf02/prgevnt/H1BNF202S00.do",
    }
    page = 1
    while True:
        payload = {"bnf02PrgEvntVo": {
            "evntCtgrNo": "", "searchKwrd": "", "sortOrd": "orderNew",
            "pageIndex": str(page), "pageSize": "15", "evntItgCfcd": "",
        }}
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        lst = r.json().get("prgEvntList", [])
        if not lst:
            break
        for it in lst:
            title = clean_title(html.unescape(it.get("cardEvntNm", "")))
            if not is_travel_event(title):
                continue
            std, edd = it.get("evntSdt", ""), it.get("evntEdt", "")
            thumb = it.get("fileCoursWeb") or ""
            if thumb.startswith("/"):
                thumb = "https://pc.wooricard.com" + thumb
            events.append({
                "카드사": "우리카드", "이벤트명": title,
                "기간": f"{std}~{edd}" if std and edd else "",
                "종료일": edd.replace(".", "-") if edd else "",
                "링크": "https://pc.wooricard.com/dcpc/yh1/bnf/bnf02/prgevnt/movePrgEvntDtl.do"
                        f"?evntSrno={it.get('evntSrno', '')}",
                "썸네일": thumb,
                "설명": clean_title(html.unescape(it.get("evntSumTxt", ""))),
                "_id": it.get("evntSrno", ""),
            })
        if lst[-1].get("addYn") != "Y":
            break
        page += 1
        if page > 30:  # 안전장치 (실제로는 3페이지 내외)
            break
    return events


def collect_hyundai_privia():
    # PRIVIA는 현대카드와 별도 도메인(priviatravel.com)의 자체 여행 서비스.
    # 프로모션 목록 페이지가 서버 렌더링이라 전체 항목이 최초 응답에 포함됨 (페이징 불필요).
    # 목록에 기간/설명 정보가 없어 채우지 않음 (추측 금지).
    events = []
    r = requests.get(
        "https://www.priviatravel.com/promotion/promotionList",
        headers={"User-Agent": UA}, timeout=15,
    )
    r.raise_for_status()
    pattern = re.compile(
        r'b-id="([^"]*)"[^>]*b-creative="([^"]*)"[^>]*b-position="[^"]*">\s*'
        r'<a href="\s*([^"]*)"[^>]*>\s*'
        r'<span class="vis"><img src="([^"]*)"',
        re.S,
    )
    seen_ids = set()
    for eid, title, link, thumb in pattern.findall(r.text):
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        title = clean_title(html.unescape(title))
        if not is_travel_event(title):
            continue
        events.append({
            "카드사": "현대카드 PRIVIA", "이벤트명": title, "기간": "",
            "종료일": "", "링크": link.strip(), "썸네일": thumb,
            "설명": "", "_id": eid,
        })
    return events


HANA_ITEM_RE = re.compile(
    r"GA_Event_Fn\('통합앱_이벤트','#[^']*','(?P<title>[^']*)','',\s*"
    r"detail\('/MKEVT1010M\.web','(?P<seq>\d+)'\)\).*?"
    r'<div class="usage-default-title[^"]*">(?P<title2>.*?)</div>\s*'
    r'<div class="usage-default-etc">\s*<div class="usage-default-etc-item">(?P<period>[^<]*)</div>',
    re.S,
)


def _hana_end_date(period: str) -> str:
    m = re.search(r"~\s*(\d{4})\.(\d{2})\.(\d{2})", period)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def collect_hanacard():
    # evnCate=00102가 '여행/해외' 탭 필터. 서버가 필터링된 HTML을 그대로 내려주므로
    # 자바스크립트 실행 불필요, 페이징 없이 목록 전량이 초기 HTML에 들어 있음.
    # 현대카드와 마찬가지로 legacy TLS 재협상이 필요.
    s = requests.Session()
    s.mount("https://", LegacyTLSAdapter())
    r = s.get(
        "https://m.hanacard.co.kr/MKEVT1000M.web",
        params={"evnCate": "00102"}, headers={"User-Agent": UA}, timeout=15,
    )
    r.raise_for_status()
    r.encoding = "euc-kr"

    events = []
    for m in HANA_ITEM_RE.finditer(r.text):
        title = clean_title(html.unescape(m.group("title2") or m.group("title") or ""))
        if not is_travel_event(title):
            continue
        seq = m.group("seq")
        period = re.sub(r"\s+", " ", m.group("period")).strip()
        events.append({
            "카드사": "하나카드", "이벤트명": title,
            "기간": period, "종료일": _hana_end_date(period),
            "링크": f"https://m.hanacard.co.kr/MKEVT1010M.web?EVN_SEQ={seq}",
            # 목록 썸네일은 제휴사 로고라 이벤트 구분이 안 됨 - 상세 페이지 .full-contents 첫
            # img로 별도 수집 필요 (신규 이벤트 push 시 에이전트가 처리)
            "썸네일": "", "설명": "", "_id": seq,
        })
    return events


COMPETITOR_SOURCES = {
    "신한카드": collect_shinhan,
    "KB국민카드": collect_kb,
    "롯데카드": collect_lotte,
    "삼성카드": collect_samsung,
    "현대카드": collect_hyundai,
    "네이버": collect_naver,
    "우리카드": collect_woori,
    "현대카드 PRIVIA": collect_hyundai_privia,
}

OWN_SOURCES = {
    "하나카드": collect_hanacard,
}

# 이벤트 그룹별 적재 대상 DB (경쟁사 DB에 자사를 섞지 않기 위한 태그)
SOURCE_GROUPS = {
    "competitor": COMPETITOR_SOURCES,
    "own": OWN_SOURCES,
}


def run_collection():
    all_events = []
    failures = []
    for group, sources in SOURCE_GROUPS.items():
        for name, fn in sources.items():
            try:
                evs = fn()
                for e in evs:
                    e["_그룹"] = group
                all_events.extend(evs)
                print(f"[수집] {name}: {len(evs)}건", file=sys.stderr)
            except Exception as e:
                failures.append((name, str(e)))
                print(f"[실패] {name}: {e}", file=sys.stderr)
    return all_events, failures


def event_key(ev):
    return f"{ev['카드사']}::{ev['_id'] or ev['이벤트명']}"


def event_group(ev):
    # prev.json에 저장된 과거 항목은 _그룹 필드가 없을 수 있으므로 카드사명으로도 판정한다
    return "own" if ev.get("카드사") in OWN_SOURCES else ev.get("_그룹", "competitor")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    events, failures = run_collection()
    EVENTS_PATH.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.diff:
        print(f"총 {len(events)}건 수집 -> {EVENTS_PATH}")
        return

    prev = {}
    if PREV_PATH.exists():
        prev = {e["_key"]: e for e in json.loads(PREV_PATH.read_text(encoding="utf-8"))}

    curr = {event_key(e): {**e, "_key": event_key(e)} for e in events}

    new_events = [curr[k] for k in curr if k not in prev]
    ended_events = [prev[k] for k in prev if k not in curr]

    print("\n[변경]")
    print(f"신규: {len(new_events)}건")
    for e in new_events:
        suffix = f" ({e['기간']})" if e["기간"] else ""
        print(f"  + [{event_group(e)}/{e['카드사']}] {e['이벤트명']}{suffix}")
    print(f"종료: {len(ended_events)}건")
    for e in ended_events:
        print(f"  - [{event_group(e)}/{e['카드사']}] {e['이벤트명']}")

    if failures:
        print("\n[실패한 소스]")
        for name, err in failures:
            print(f"  {name}: {err}")

    PREV_PATH.write_text(
        json.dumps(list(curr.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.push:
        def split(lst):
            return (
                [e for e in lst if event_group(e) == "competitor"],
                [e for e in lst if event_group(e) == "own"],
            )

        new_competitor, new_own = split(new_events)
        ended_competitor, ended_own = split(ended_events)
        queue = {
            "competitor": {"new": new_competitor, "ended": ended_competitor},
            "own": {"new": new_own, "ended": ended_own},
        }
        PUSH_QUEUE_PATH.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n-> {PUSH_QUEUE_PATH} 생성됨 (에이전트가 이 파일을 읽어 Notion에 반영)")


if __name__ == "__main__":
    main()
