"""인사이슈 보드 — 매일 자동수집 스크립트 (GitHub Actions에서 실행)

출처별로 가져와서 data/YYYY-MM.json 에 쌓는다.
- 키는 환경변수(GitHub 비밀값)로만 받는다. 저장 파일·링크에는 절대 넣지 않는다.
- 한 출처가 실패해도 나머지는 저장하고, 실패 내역은 data/index.json 에 남긴다.
- 외부 라이브러리 없이 파이썬 기본 기능만 사용한다.
"""
import json, os, re, sys, html, glob, datetime, urllib.request, urllib.parse
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
KST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime.now(KST)
TODAY = NOW.date()

CFG = json.load(open(os.path.join(ROOT, "config", "settings.json"), encoding="utf-8"))
ENV = os.environ.get
NOTES = {}   # 출처별 부가 안내 (예: 개정이유 일부 실패)


# ───────────────────────── 공통 ─────────────────────────
def http_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "hr-issue-board/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def clean(s):
    """태그 제거 + HTML 엔티티 복원 + 공백 정리"""
    s = re.sub(r"<[^>]+>", "", str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def ymd(s):
    """'20261104' / '2026-11-04' / '2026.11.04' → '2026-11-04', 실패 시 ''"""
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) >= 8:
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return ""


def tool_for(text):
    hits = [t["name"] for t in CFG["tools"] if any(m in text for m in t["match"])]
    return ", ".join(hits)


def item(**kw):
    base = {"id": "", "cat": "", "title": "", "date": "", "source": "", "url": "",
            "summary": "", "effDate": "", "stage": "", "stageChanged": False,
            "tool": "", "collected": TODAY.isoformat()}
    base.update(kw)
    base["tool"] = base["tool"] or tool_for(base["title"])
    return base


# ───────────────────────── 국회 의안 ─────────────────────────
ASSEMBLY_URL = "https://open.assembly.go.kr/portal/openapi/nzmimeepazxkubdpn"


def bill_stage(r):
    if r.get("PROC_RESULT"):
        return "본회의 " + r["PROC_RESULT"]
    if r.get("LAW_PROC_DT"):
        return "법사위 심사"
    if r.get("CMT_PROC_DT"):
        return "위원회 의결" + (f"({r['CMT_PROC_RESULT_CD']})" if r.get("CMT_PROC_RESULT_CD") else "")
    if r.get("COMMITTEE_DT") or r.get("COMMITTEE"):
        return "위원회 심사"
    return "발의"


def bill_rows(key, kw, max_pages=15):
    """한 키워드의 의안을 페이지를 넘기며 전부 가져온다 (100건 제한 해제)"""
    rows = []
    for page in range(1, max_pages + 1):
        q = urllib.parse.urlencode({"KEY": key, "Type": "json", "pIndex": page, "pSize": 100,
                                    "AGE": CFG["assembly_age"], "BILL_NAME": kw})
        js = http_json(f"{ASSEMBLY_URL}?{q}")
        block = js.get("nzmimeepazxkubdpn")
        if not block:  # 결과 없음 (INFO-200) 또는 오류
            res = js.get("RESULT", {})
            if res.get("CODE") not in (None, "INFO-200"):
                raise RuntimeError(f"국회 API 오류: {res.get('CODE')} {res.get('MESSAGE')}")
            break
        got = block[1].get("row", []) if len(block) > 1 else []
        rows += got
        if len(got) < 100:
            break
    return rows


def collect_bills(key, prev_stages):
    out = []
    for kw in CFG["bill_keywords"]:
        for r in bill_rows(key, kw):
            bid = r.get("BILL_ID") or r.get("BILL_NO")
            if not bid:
                continue
            stage = bill_stage(r)
            out.append(item(
                id=f"bill:{bid}", cat="bill", title=clean(r.get("BILL_NAME")),
                date=ymd(r.get("PROPOSE_DT")), source=clean(r.get("PROPOSER")) or "국회",
                url=r.get("DETAIL_LINK") or "", stage=stage,
                stageChanged=(bid in prev_stages and prev_stages[bid] != stage),
                summary=clean(r.get("COMMITTEE")) and f"소관: {clean(r.get('COMMITTEE'))}"))
    return out


# ───────────────────────── 법제처 (법령·판례) ─────────────────────────
LAW_URL = "https://www.law.go.kr/DRF/lawSearch.do"


def law_search(oc, target, query, extra=None):
    p = {"OC": oc, "target": target, "type": "JSON", "query": query, "display": 20}
    p.update(extra or {})
    js = http_json(f"{LAW_URL}?{urllib.parse.urlencode(p)}")
    root = js.get("LawSearch") or js.get("PrecSearch") or {}
    rows = root.get(target) or []
    return rows if isinstance(rows, list) else [rows]


def _strings(o):
    """중첩된 dict/list 안의 문자열을 순서대로 모두 꺼낸다"""
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def _find_key(o, words):
    """키 이름에 words 중 하나가 들어간 첫 값을 찾는다 (응답 구조가 조금 달라도 동작하게)"""
    if isinstance(o, dict):
        for k, v in o.items():
            if any(w in k for w in words) and not k.endswith("여부"):
                return v
        for v in o.values():
            r = _find_key(v, words)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_key(v, words)
            if r is not None:
                return r
    return None


def law_reason(oc, mst):
    """법령 본문에서 '제·개정이유 및 주요내용'을 꺼낸다 (현행법령 본문 JSON 권한 필요)"""
    q = urllib.parse.urlencode({"OC": oc, "target": "law", "MST": mst, "type": "JSON"})
    js = http_json(f"https://www.law.go.kr/DRF/lawService.do?{q}", timeout=40)
    part = _find_key(js, ["제개정이유", "개정이유"]) or _find_key(js, ["개정문"])
    if part is None:
        return ""
    lines = [clean(s) for s in _strings(part)]
    text = "\n".join(s for s in lines if s)
    return text[:4000]


def collect_laws(oc, existing=None):
    existing = existing or {}
    out = []
    for name in CFG["laws"]:
        for r in law_search(oc, "law", name, {"sort": "ddes"}):
            title = clean(r.get("법령명한글"))
            if name not in title:
                continue
            seq = r.get("법령일련번호") or r.get("법령ID")
            kind = clean(r.get("제개정구분명"))
            out.append(item(
                id=f"law:{seq}", cat="law", title=f"{title} {kind}".strip(),
                date=ymd(r.get("공포일자")), effDate=ymd(r.get("시행일자")),
                source=clean(r.get("소관부처명")) or "법제처",
                # 공개 링크만 저장 (OC가 들어간 API 링크는 저장하지 않음)
                url="https://www.law.go.kr/법령/" + urllib.parse.quote(title),
                mst=str(seq),
                summary=f"공포번호 {clean(r.get('공포번호'))}" if r.get("공포번호") else ""))

    # 개정이유: 이미 받아둔 건 다시 받지 않음. 본문 권한이 없거나 실패해도 목록 수집은 유지
    reason_fail = 0
    for it in out:
        old = existing.get(it["id"], (None, {}))[1]
        if old.get("reason"):
            it["reason"] = old["reason"]
            continue
        try:
            it["reason"] = law_reason(oc, it["mst"])
        except Exception as e:
            reason_fail += 1
            print(f"[개정이유] {it['title']}: {e}", file=sys.stderr)
    if reason_fail:
        print(f"[개정이유] {reason_fail}건 실패", file=sys.stderr)
    NOTES["법령"] = f"개정이유 {reason_fail}건 실패" if reason_fail else ""
    return out


def collect_cases(oc):
    out = []
    for kw in CFG["case_keywords"]:
        # search=2: 사건명이 아니라 판결문 본문에서 검색 (통상임금·괴롭힘 사건은 사건명이 '임금', '해고무효확인'이라서)
        # display=100: 키워드당 최신 100건까지
        for r in law_search(oc, "prec", kw, {"sort": "ddes", "search": 2, "display": 100}):
            seq = r.get("판례일련번호")
            if not seq:
                continue
            out.append(item(
                id=f"case:{seq}", cat="case",
                title=f"{clean(r.get('사건명'))} ({clean(r.get('사건번호'))})",
                date=ymd(r.get("선고일자")), source=clean(r.get("법원명")) or "법원",
                url=f"https://www.law.go.kr/precInfoP.do?precSeq={seq}",
                topic=kw, summary=f"검색어: {kw}"))
    return out


# ───────────────────────── 네이버 뉴스 ─────────────────────────
def news_ok(title, host):
    """제목 기준 필터: 본문에만 검색어가 스친 기사는 버린다"""
    t = title.replace(" ", "")
    if any(w.replace(" ", "") in t for w in CFG.get("news_exclude", [])):
        return False
    if any(w.replace(" ", "") in t for w in CFG.get("news_title_keywords", [])):
        return True
    trusted = any(host.endswith(d) for d in CFG.get("news_trusted", []))
    return trusted and any(w in t for w in CFG.get("news_trusted_keywords", []))


def collect_news(cid, secret):
    out = []
    for q in CFG["news_queries"]:
        u = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
            {"query": q, "display": 20, "sort": "date"})
        js = http_json(u, {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret})
        for r in js.get("items", []):
            link = r.get("originallink") or r.get("link")
            try:
                d = parsedate_to_datetime(r.get("pubDate")).astimezone(KST).date().isoformat()
            except Exception:
                d = TODAY.isoformat()
            host = urllib.parse.urlparse(link).netloc.replace("www.", "")
            if not news_ok(clean(r.get("title")), host):
                continue
            desc = clean(r.get("description"))
            out.append(item(
                id=f"news:{link}", cat="news", title=clean(r.get("title")),
                date=d, source=host, url=link,
                summary=(desc[:90] + "…") if len(desc) > 90 else desc,  # 본문 저장 안 함
                tool=""))  # 뉴스는 도구 영향 판정에서 제외
    return out


# ───────────────────────── 저장 ─────────────────────────
def load_existing():
    by_id = {}
    for f in sorted(glob.glob(os.path.join(DATA, "20??-??.json"))):
        for it in json.load(open(f, encoding="utf-8")):
            by_id[it["id"]] = (os.path.basename(f), it)
    return by_id


def save(new_items, existing):
    os.makedirs(DATA, exist_ok=True)
    month_file = f"{TODAY:%Y-%m}.json"
    months = {}
    for fname, it in existing.values():
        months.setdefault(fname, {})[it["id"]] = it

    added = changed = 0
    seen = set()
    for it in new_items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        if it["id"] in existing:
            fname, old = existing[it["id"]]
            # 기존 항목에 없던 정보(개정이유 등)가 새로 생겼으면 채워 넣기
            for k in ("reason", "mst", "topic"):
                if it.get(k) and not old.get(k):
                    months[fname][it["id"]][k] = it[k]
            # 의안 상태 변화: 단계가 바뀌면 내용 갱신 + 오늘 수집분으로 다시 올림
            if it["cat"] == "bill" and old.get("stage") != it["stage"]:
                it["stageChanged"] = True
                months[fname].pop(it["id"], None)
                months.setdefault(month_file, {})[it["id"]] = it
                changed += 1
            continue
        months.setdefault(month_file, {})[it["id"]] = it
        added += 1

    # 뉴스 정리: 보관기간 초과분 + 필터 기준에 안 맞는 기존 기사 (필터를 바꾸면 과거분도 같이 정리됨)
    cutoff = (TODAY - datetime.timedelta(days=CFG["news_keep_days"])).isoformat()
    for fname in list(months):
        months[fname] = {k: v for k, v in months[fname].items()
                         if not (v["cat"] == "news" and
                                 (v["collected"] < cutoff or not news_ok(v["title"], v["source"])))}
        path = os.path.join(DATA, fname)
        rows = sorted(months[fname].values(), key=lambda x: (x["collected"], x["date"]), reverse=True)
        json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    return added, changed, sorted(months, reverse=True)


def main():
    existing = load_existing()
    try:
        old_idx = json.load(open(os.path.join(DATA, "index.json"), encoding="utf-8"))
    except Exception:
        old_idx = {}
    prev_stages = {i["id"][5:]: i["stage"] for _, i in existing.values() if i["cat"] == "bill"}
    results, status = [], {}

    jobs = [
        ("의안", ["ASSEMBLY_KEY"], lambda: collect_bills(ENV("ASSEMBLY_KEY"), prev_stages)),
        ("법령", ["LAW_OC"], lambda: collect_laws(ENV("LAW_OC"), existing)),
        ("판례", ["LAW_OC"], lambda: collect_cases(ENV("LAW_OC"))),
        ("뉴스", ["NAVER_ID", "NAVER_SECRET"], lambda: collect_news(ENV("NAVER_ID"), ENV("NAVER_SECRET"))),
    ]
    for name, keys, fn in jobs:
        if not all(ENV(k) for k in keys):
            status[name] = "키 미등록"
            continue
        try:
            got = fn()
            results += got
            status[name] = f"정상 {len({g['id'] for g in got})}건"
            if NOTES.get(name):
                status[name] += f" · {NOTES[name]}"
        except Exception as e:  # 한 출처 실패가 전체를 멈추지 않게
            msg = str(e)
            if "403" in msg:
                msg = "403 접근 거부 (IP 미등록일 수 있음)"
            status[name] = "실패: " + msg[:120]
            print(f"[{name}] {msg}", file=sys.stderr)

    added, changed, months = save(results, existing)
    total = sum(len(json.load(open(os.path.join(DATA, m), encoding="utf-8"))) for m in months)
    index = {"lastRun": NOW.strftime("%Y-%m-%d %H:%M"), "today": TODAY.isoformat(),
             # 첫 수집 날은 기존 자료가 한꺼번에 들어오므로 보드에서 NEW 표시를 끈다
             "firstRun": len(existing) == 0,
             # 첫 수집 날짜(일괄 수집분) — 이 날 들어온 자료는 메인 화면 '최근'에서 제외
             "backfillDate": old_idx.get("backfillDate")
                             or (old_idx.get("today") if old_idx.get("firstRun") else None)
                             or (TODAY.isoformat() if len(existing) == 0 else None),
             "months": months, "total": total, "added": added, "stageChanged": changed,
             "status": status}
    json.dump(index, open(os.path.join(DATA, "index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(index, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
