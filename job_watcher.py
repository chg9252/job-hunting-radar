#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
platform_watcher — 채용 플랫폼(platform.com) 채용 공고를 내 이력에 대입해 매칭 공고를 알려주는 워처.

파이프라인:
  1) 공개 API(https://api.example.com/api/recruitments)에서 keyword별로 공고 목록 수집
  2) 직무(depthTwos)·경력·지역으로 규칙 기반 프리필터 + 점수화
  3) 이전 실행에서 본 id와 diff → "새로 뜬" 공고만 추림
  4) 규칙 문턱 통과 + 신규 공고만 상세 API 조회 → LLM(Claude)이 이력서와 정밀 대조해 재점수
  5) Obsidian 노트(matches/YYYY-MM-DD.md)로 매칭 다이제스트 출력 + 본 id 저장

의존성: 표준 라이브러리만으로 규칙 기반 동작. LLM 단계는 `anthropic` + ANTHROPIC_API_KEY 있을 때만.
사용법:  python platform_watcher.py           (전체 실행)
         python platform_watcher.py --no-llm  (규칙 점수만)
         python platform_watcher.py --seed    (알림 없이 현재 공고를 seen에 기록만, 첫 세팅용)
         python platform_watcher.py --dry-run (노트/상태 저장 안 함, 콘솔 출력만)
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# ---- Windows 콘솔에서 한글 깨짐 방지 ----
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
API_LIST = "https://api.example.com/api/recruitments"
API_DETAIL = "https://api.example.com/api/recruitments/{id}"
DETAIL_PAGE = "https://platform.com/recruitment/{id}"
UA = "Mozilla/5.0 (platform-watcher; personal job-match tool)"


# =========================================================================
# 유틸
# =========================================================================
def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_dotenv(path=None):
    """스크립트 폴더의 .env를 읽어 os.environ에 주입(이미 있는 값은 안 덮음).

    python-dotenv 없이 표준 라이브러리만으로 동작. 파일 없으면 조용히 통과.
    형식: KEY=VALUE (한 줄에 하나, # 주석·빈 줄 무시, 따옴표 자동 제거).
    """
    path = path or os.path.join(HERE, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:  # noqa: BLE001
        pass


def load_config(path="config.json"):
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_profile(cfg):
    path = os.path.join(HERE, cfg["output"]["profile_file"])
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def http_get_json(url, retries=3, pause=1.5):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(pause * (i + 1))
    raise RuntimeError(f"GET 실패: {url} :: {last}")


def http_post_json(url, data, timeout=15):
    """폼 인코딩 POST → JSON 응답. 텔레그램 sendMessage 등에 사용."""
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_multipart(url, fields, files, timeout=60):
    """multipart/form-data POST(파일 업로드) → JSON. 텔레그램 sendDocument용.

    fields: {name: str}, files: {name: (filename, bytes, mime)}.
    """
    boundary = "----zw" + os.urandom(8).hex()
    crlf = "\r\n"
    body = b""
    for name, val in fields.items():
        body += (f"--{boundary}{crlf}"
                 f'Content-Disposition: form-data; name="{name}"{crlf}{crlf}'
                 f"{val}{crlf}").encode("utf-8")
    for name, (fname, content, mime) in files.items():
        body += (f"--{boundary}{crlf}"
                 f'Content-Disposition: form-data; name="{name}"; filename="{fname}"{crlf}'
                 f"Content-Type: {mime}{crlf}{crlf}").encode("utf-8")
        body += content + crlf.encode("utf-8")
    body += f"--{boundary}--{crlf}".encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# =========================================================================
# 1) 수집
# =========================================================================
# 채용 플랫폼 사이트 필터 = API 파라미터. 리스트형(다중값) + 스칼라형.
_LIST_FILTER_KEYS = ("depthOnes", "depthTwos", "regions", "employeeTypes",
                     "educations", "companyTypes", "deadlineTypes")
_SCALAR_FILTER_KEYS = ("careerMin", "careerMax")


def build_filter_qs(filters):
    """config search.filters → 채용 플랫폼 필터 쿼리스트링. 빈 값은 생략(= 전체)."""
    parts = []
    for key in _LIST_FILTER_KEYS:
        for v in filters.get(key) or []:
            parts.append(f"{key}={urllib.parse.quote(str(v))}")
    for key in _SCALAR_FILTER_KEYS:
        if filters.get(key) is not None:
            parts.append(f"{key}={filters[key]}")
    return "&".join(parts)


def fetch_listings(cfg):
    """채용 플랫폼 필터(서버단) + 선택적 keyword로 페이지를 긁어 id 기준 dedupe."""
    s = cfg["search"]
    fqs = build_filter_qs(s.get("filters") or {})
    max_pages = s.get("max_pages", s.get("pages_per_keyword", 8))
    # keywords 있으면 각 키워드로 추가 검색, 없으면 필터만으로 1회 순회
    keywords = s.get("keywords") or [None]
    by_id = {}
    for kw in keywords:
        label = kw or "(필터만)"
        total = "?"
        for pg in range(max_pages):
            bits = [fqs, f"size={s['page_size']}", f"page={pg}"]
            if s.get("sort"):
                bits.append(f"sort={s['sort']}")
            if kw:
                bits.append(f"keyword={urllib.parse.quote(kw)}")
            url = f"{API_LIST}?" + "&".join(b for b in bits if b)
            try:
                data = http_get_json(url).get("data") or {}
            except RuntimeError as e:
                log(f"  ! '{label}' p{pg} 수집 실패: {e}")
                break
            total = data.get("totalElements", total)
            content = data.get("content") or []
            for it in content:
                by_id[it["id"]] = it
            if data.get("last") or not content:
                break
        log(f"  · '{label}' 필터 대상 {total}건 → 누적 수집 {len(by_id)}건")
    return list(by_id.values())


def fetch_detail(rid):
    data = http_get_json(API_DETAIL.format(id=rid)).get("data") or {}
    return data


def flatten_content(node, out):
    """상세의 TipTap/ProseMirror content(JSON doc)를 평문으로."""
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text"):
            out.append(node["text"])
        for v in node.get("content", []) or []:
            flatten_content(v, out)
    elif isinstance(node, list):
        for v in node:
            flatten_content(v, out)


def created_age_days(created_at):
    """createdAt(ISO) → 오늘까지 경과일. 파싱 실패 시 None."""
    if not created_at:
        return None
    try:
        d0 = dt.date.fromisoformat(str(created_at)[:10])
    except ValueError:
        return None
    return (dt.date.today() - d0).days


def detail_to_text(detail):
    parts = []
    flatten_content(detail.get("content"), parts)
    body = " ".join(parts)
    kw = detail.get("keywords") or []
    if kw:
        body += "\n[태그] " + ", ".join(kw)
    return body.strip()


# =========================================================================
# 1-b) enrich: 채용 플랫폼 본문이 빈약하면 원본(채용사이트·기업 채용페이지)을 crawl4ai로 크롤
# =========================================================================
def crawl_originals(url_by_id, page_timeout_sec=45):
    """{id: redirectUrl} → {id: 정제 JD 마크다운}. crawl4ai(로컬 헤드리스)로 SPA 렌더링.

    crawl4ai 미설치·크롤 실패는 조용히 건너뜀(해당 id는 결과에 없음 → 폴백).
    """
    if not url_by_id:
        return {}
    try:
        import asyncio  # noqa: WPS433
        from crawl4ai import (  # noqa: WPS433
            AsyncWebCrawler, BrowserConfig, CrawlerRunConfig,
        )
        from crawl4ai.content_filter_strategy import PruningContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    except ImportError:
        log("  · enrich 생략: crawl4ai 미설치 (pip install crawl4ai)")
        return {}

    async def _run():
        out = {}
        bcfg = BrowserConfig(headless=True, verbose=False)
        mdgen = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=0.48))
        run = CrawlerRunConfig(page_timeout=page_timeout_sec * 1000,
                               wait_until="networkidle",
                               markdown_generator=mdgen, verbose=False)
        async with AsyncWebCrawler(config=bcfg) as crawler:
            for rid, url in url_by_id.items():
                try:
                    r = await crawler.arun(url=url, config=run)
                    md = getattr(r.markdown, "fit_markdown", "") or \
                        getattr(r.markdown, "raw_markdown", "") or ""
                    if r.success and len(md) > 200:
                        out[rid] = md[:6000]
                except Exception as e:  # noqa: BLE001
                    log(f"  ! 원본 크롤 실패({rid[:8]}): {str(e)[:80]}")
        return out

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        log(f"  · enrich 생략: 크롤러 구동 실패 ({str(e)[:80]})")
        return {}


# =========================================================================
# 2) 규칙 기반 점수
# =========================================================================
def score_role(item, f):
    d2 = set(item.get("depthTwos") or [])
    d1 = set(item.get("depthOnes") or [])
    if d2 & set(f["primary_depth_twos"]):
        return 1.0
    if d2 & set(f["secondary_depth_twos"]):
        return 0.6
    if d1 & set(f["target_depth_ones"]):
        return 0.35
    return 0.0


def score_career(item, years, tol_over):
    cmin = item.get("careerMin")
    cmax = item.get("careerMax")
    if cmin is None and cmax is None:
        return 0.7  # 경력 무관
    cmin = 0 if cmin is None else cmin
    cmax = 100 if cmax is None else cmax
    if cmin <= years <= cmax:
        return 1.0
    if years < cmin:  # 공고가 더 높은 경력 요구
        gap = cmin - years
        if gap <= tol_over:
            return max(0.3, 1.0 - 0.35 * gap)
        return 0.1
    # years > cmax : 내가 더 시니어 (신입~주니어 공고)
    if cmax >= 2:
        return 0.5
    return 0.3


def score_region(item, preferred):
    regions = item.get("regions") or []
    if not regions:
        return 0.8
    if any(any(p in r for p in preferred) for r in regions):
        return 1.0
    return 0.3


def score_employment(item):
    ets = item.get("employeeTypes") or []
    if any("정규" in e for e in ets):
        return 1.0
    if not ets:
        return 0.7
    return 0.4


def score_tech_title(item, prof):
    title = (item.get("title") or "").lower()
    p = sum(1 for t in prof["tech_primary"] if t in title)
    s = sum(1 for t in prof["tech_secondary"] if t in title)
    return min(1.0, 0.5 * p + 0.25 * s)


def title_excluded(item, f):
    title = (item.get("title") or "").lower()
    return any(x.lower() in title for x in f["exclude_title_keywords"])


def rule_score(item, cfg):
    f, prof, sc = cfg["filter"], cfg["profile"], cfg["scoring"]
    parts = {
        "role": score_role(item, f),
        "career": score_career(item, prof["career_years"], sc["career_tolerance_over"]),
        "tech_title": score_tech_title(item, prof),
        "region": score_region(item, f["regions_preferred"]),
        "employment": score_employment(item),
    }
    w = sc["weights"]
    total = sum(parts[k] * w[k] for k in parts) / sum(w.values()) * 100
    return round(total, 1), parts


# =========================================================================
# 4) LLM 정밀 점수 (선택)
# =========================================================================
LLM_SYS = (
    "너는 시니어 백엔드 채용 매칭 어시스턴트다. 지원자 이력과 채용 공고를 대조해 "
    "적합도를 0~100으로 냉정하게 평가한다. 과장 없이, 공고에 명시된 요구사항 기준으로만 판단한다. "
    "반드시 JSON만 출력한다."
)


def resolve_engine(cfg, no_llm):
    """어떤 LLM 백엔드를 쓸지 결정. 반환: (engine dict | None, 사유 str | None).

    engine = {"provider": "api"|"claude_cli", ...}
    - claude_cli: 지금 쓰는 Claude Code 구독으로 채점(별도 키 불필요).
    - api: anthropic SDK + ANTHROPIC_API_KEY.
    """
    if no_llm:
        return None, "--no-llm 플래그"
    llm = cfg["llm"]
    if not llm.get("enabled"):
        return None, "config에서 llm.enabled=false"
    provider = llm.get("provider", "claude_cli")
    if provider == "claude_cli":
        import shutil  # noqa: WPS433
        exe = shutil.which("claude") or shutil.which("claude.cmd")
        if not exe:
            return None, "claude CLI를 PATH에서 못 찾음"
        return {"provider": "claude_cli", "exe": exe,
                "model": llm.get("cli_model", "sonnet")}, None
    if provider == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None, "ANTHROPIC_API_KEY 환경변수 없음"
        try:
            import anthropic  # noqa: WPS433
        except ImportError:
            return None, "anthropic 패키지 미설치 (pip install anthropic)"
        return {"provider": "api", "client": anthropic.Anthropic(),
                "model": llm.get("model", "claude-sonnet-5")}, None
    return None, f"알 수 없는 provider: {provider}"


def build_prompt(profile_text, item, detail_text):
    schema_hint = (
        '{"score": <0-100 정수>, "verdict": "<강력추천|추천|보통|낮음>", '
        '"reasons": ["부합 근거 최대3"], "gaps": ["부족/리스크 최대3"], '
        '"one_liner": "<한 줄 총평>", '
        '"deadline": "<지원 마감일: 구체 날짜면 YYYY-MM-DD, 상시채용/채용시 마감이면 \'상시\', 본문에 없으면 \'미상\'>", '
        '"strategy": ["지원·합격 전략 2~4개(구체적으로): 서류에서 강조할 이력·키워드, '
        '이력서/자기소개서 각색 포인트, 면접 대비 포인트, gaps 보완·프레이밍 방법"]}'
    )
    return (
        f"## 지원자 이력\n{profile_text}\n\n"
        f"## 채용 공고\n"
        f"회사: {item.get('company',{}).get('name')}\n"
        f"제목: {item.get('title')}\n"
        f"직무: {', '.join(item.get('depthTwos') or [])}\n"
        f"경력: {item.get('careerMin')}~{item.get('careerMax')}년 / 지역: {', '.join(item.get('regions') or [])}\n\n"
        f"### 공고 상세\n"
        f"(원문 크롤 결과라 사이트 네비게이션·로그인·회원가입·푸터·다른 공고 목록 같은 "
        f"잡음이 섞였을 수 있다. 그런 부분은 무시하고 이 공고의 실제 채용 요구사항"
        f"(담당업무·자격요건·우대사항·기술스택)만 근거로 삼아 판단하라.)\n"
        f"{detail_text[:6000]}\n\n"
        f"위 지원자가 이 공고에 지원했을 때의 서류 통과 가능성과 직무 적합도를 평가하라.\n"
        f"**중요 — 필수와 우대를 구분해 가중치를 다르게 매겨라**: "
        f"공고의 요구사항을 '필수(자격요건·필수·지원자격·Requirements)'와 "
        f"'우대(우대사항·있으면 좋음·Preferred·Nice to have)'로 나눠라. "
        f"필수 미충족은 서류 통과에 실질적 감점이다. 그러나 **우대 미충족은 경미하게** 평가하라 — "
        f"대용량 트래픽 처리·특정 프레임워크·특정 인프라 같은 우대 역량은 "
        f"보통 '입사해서 하게 되는 일'이지 입사 전 필수 조건이 아닌 경우가 많다. "
        f"필수를 충족하면 우대가 여럿 비어도 지원 가치는 충분할 수 있다. "
        f"우대 역량 부족을 필수 결격처럼 과하게 깎지 마라.\n"
        f"strategy에는 이 지원자가 이 공고에 실제로 지원한다면 어떻게 어필하고 준비해야 합격 확률이 높아질지 "
        f"실행 가능한 조언을 담아라.\n"
        f"deadline은 공고 본문에서 지원(서류) 마감일을 찾아 넣어라. "
        f"'2026-07-31'처럼 구체 날짜가 있으면 그 날짜(YYYY-MM-DD)를, "
        f"'상시채용'·'채용 시 마감'·'수시'면 '상시'를, 아무 언급이 없으면 '미상'을 넣어라. 추측하지 마라.\n"
        f"다음 JSON 형식만 출력:\n{schema_hint}"
    )


def _extract_json(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError(f"LLM 응답에서 JSON 못 찾음: {txt[:200]}")
    return json.loads(m.group(0))


def _score_api(engine, max_tokens, prompt):
    resp = engine["client"].messages.create(
        model=engine["model"], max_tokens=max_tokens,
        system=LLM_SYS,
        messages=[{"role": "user", "content": prompt}],
    )
    txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _extract_json(txt)


# claude CLI를 볼트 밖 중립 폴더에서 실행 → 볼트 CLAUDE.md/스킬 미로드로 오버헤드 감소
import tempfile  # noqa: E402
CLI_CWD = os.path.join(tempfile.gettempdir(), "platform_watcher_cli")


def _score_cli(engine, max_tokens, prompt):
    import subprocess  # noqa: WPS433
    os.makedirs(CLI_CWD, exist_ok=True)
    full = LLM_SYS + "\n\n" + prompt
    proc = subprocess.run(
        [engine["exe"], "-p", "--output-format", "json", "--model", engine["model"]],
        input=full, capture_output=True, text=True, encoding="utf-8",
        cwd=CLI_CWD, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI 실패(rc={proc.returncode}): {(proc.stderr or '')[:200]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        raise RuntimeError(f"claude CLI 오류 응답: {str(envelope)[:200]}")
    return _extract_json(envelope["result"])


def llm_score(engine, max_tokens, profile_text, item, detail_text):
    prompt = build_prompt(profile_text, item, detail_text)
    if engine["provider"] == "api":
        return _score_api(engine, max_tokens, prompt)
    return _score_cli(engine, max_tokens, prompt)


# =========================================================================
# 3) 상태 (seen)
# =========================================================================
def load_seen(cfg):
    path = os.path.join(HERE, cfg["output"]["state_file"])
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(cfg, seen):
    path = os.path.join(HERE, cfg["output"]["state_file"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 오래된 기록 정리
    keep = cfg["output"].get("seen_retention_days", 90)
    cutoff = (dt.date.today() - dt.timedelta(days=keep)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("first_seen", "9999") >= cutoff}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


# =========================================================================
# 5) 출력 노트
# =========================================================================
def apply_url(item, detail=None):
    if detail and detail.get("redirectUrl"):
        return detail["redirectUrl"]
    return DETAIL_PAGE.format(id=item["id"])


def _build_match(it, rsc, detail, age, llm_out, jd_source):
    """공고 1건의 match dict 생성(LLM 결과 유무 공통)."""
    return {
        "id": it["id"],
        "company": it.get("company", {}).get("name", "?"),
        "title": it.get("title", "?"),
        "career": f"{it.get('careerMin','?')}~{it.get('careerMax','?')}년",
        "region": ", ".join(it.get("regions") or []) or "-",
        "depth": ", ".join(it.get("depthTwos") or []) or "-",
        "age": age,
        "url": apply_url(it, detail),
        "jd_source": jd_source,
        "rule": rsc,
        "llm": llm_out["score"] if llm_out else None,
        "score": llm_out["score"] if llm_out else rsc,
        "verdict": llm_out.get("verdict") if llm_out else None,
        "reasons": llm_out.get("reasons") if llm_out else None,
        "gaps": llm_out.get("gaps") if llm_out else None,
        "one_liner": llm_out.get("one_liner") if llm_out else None,
        "deadline": llm_out.get("deadline") if llm_out else None,
        "strategy": llm_out.get("strategy") if llm_out else None,
    }


def deadline_info(deadline):
    """마감일 문자열 → (표시라벨, 남은일수|None, 상태).

    상태: 'date'(구체 날짜) | 'rolling'(상시) | 'unknown'(미상/없음).
    구체 날짜면 D-day 계산(음수면 마감 지남).
    """
    s = (deadline or "").strip()
    if not s or s in ("미상", "unknown"):
        return "미상", None, "unknown"
    if any(t in s for t in ("상시", "채용시", "채용 시", "수시", "충원")):
        return "상시", None, "rolling"
    try:
        d = dt.date.fromisoformat(s[:10])
    except ValueError:
        return s, None, "unknown"
    days = (d - dt.date.today()).days
    md = d.strftime("%m/%d")
    if days < 0:
        return f"마감({md})", days, "date"
    if days == 0:
        return f"오늘마감({md})", 0, "date"
    return f"D-{days} ({md})", days, "date"


def write_note(cfg, matches, stats):
    today = dt.date.today().isoformat()
    out_dir = os.path.join(HERE, cfg["output"]["matches_dir"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{today}.md")
    minsc = cfg["output"]["min_score_in_note"]
    notify = cfg["scoring"]["notify_threshold"]

    shown = [m for m in matches if m["score"] >= minsc]
    shown.sort(key=lambda m: m["score"], reverse=True)

    lines = []
    person = cfg.get("name", "")
    lines.append("---")
    lines.append(f"date: {today}")
    lines.append("type: job-match-digest")
    if person:
        lines.append(f"person: {person}")
    lines.append(f"window_days: {stats.get('window', '?')}")
    lines.append(f"scanned: {stats['scanned']}")
    lines.append(f"fresh: {stats['fresh']}")
    lines.append(f"matched: {len(shown)}")
    lines.append("tags: [job-hunt, platform]")
    lines.append("---")
    lines.append("")
    title_who = f"{person} · " if person else ""
    lines.append(f"# 🎯 {title_who}채용 플랫폼 매칭 공고 — {today}")
    lines.append("")
    enr = stats.get("enriched", 0)
    enr_s = f"원본크롤 {enr}건 · " if enr else ""
    wd = stats.get("window", "?")
    lines.append(
        f"> **최근 {wd}일 등록 공고** 대상 · 스캔 {stats['scanned']}건 · 규칙통과 {stats['rule_pass']}건 · "
        f"신규 {stats['fresh']}건 · LLM평가 {stats['llm_scored']}건 · "
        f"{enr_s}노트수록 {len(shown)}건 (점수≥{minsc})"
    )
    if stats.get("llm_note"):
        lines.append(f">")
        lines.append(f"> ⚠️ LLM 단계 생략: {stats['llm_note']} (규칙 점수만 표시)")
    lines.append("")

    if not shown:
        lines.append("_오늘은 문턱을 넘는 신규 매칭 공고가 없어요._")
    else:
        lines.append("| 점수 | 판정 | 마감 | 회사 | 공고 | 경력 | 지역 |")
        lines.append("|---:|:--:|:--:|---|---|:--:|:--:|")
        for m in shown:
            flag = "🔥" if m["score"] >= notify else ""
            verdict = m.get("verdict") or ("⚙️ LLM 미검증" if m.get("llm") is None else "-")
            dlabel, ddays, dstate = deadline_info(m.get("deadline"))
            dl_cell = f"⏰{dlabel}" if (dstate == "date" and ddays is not None and ddays <= 3) else dlabel
            lines.append(
                f"| **{m['score']}**{flag} | {verdict} | {dl_cell} | {m['company']} "
                f"| [{m['title']}]({m['url']}) | {m['career']} | {m['region']} |"
            )
        lines.append("")
        lines.append("---")
        lines.append("")
        for m in shown:
            flag = " 🔥" if m["score"] >= notify else ""
            lines.append(f"## {m['score']}점{flag} · {m['company']} — {m['title']}")
            lines.append("")
            age = m.get("age")
            age_s = f" | 등록 {age}일 전" if age is not None else ""
            src = m.get("jd_source")
            src_badge = {
                "원본크롤": " · 🔎 원본 JD 크롤 반영",
                "채용 플랫폼-빈약": " · ⚠️ 채용 플랫폼에 상세 없음(원본 링크 직접 확인 권장)",
            }.get(src, "")
            lines.append(f"- 🔗 [지원/공고 링크]({m['url']}){src_badge}")
            lines.append(f"- 직무: {m['depth']} | 경력: {m['career']} | 지역: {m['region']}{age_s}")
            dlabel, ddays, dstate = deadline_info(m.get("deadline"))
            urgent = " ⏰**마감임박**" if (dstate == "date" and ddays is not None and 0 <= ddays <= 3) else ""
            lines.append(f"- 🗓️ 서류 마감: {dlabel}{urgent}")
            lines.append(
                f"- 규칙점수 {m['rule']}"
                + (f" → LLM {m['llm']}" if m.get("llm") is not None else "")
            )
            if m.get("one_liner"):
                lines.append(f"- 총평: {m['one_liner']}")
            if m.get("reasons"):
                lines.append(f"- ✅ 부합: " + " / ".join(m["reasons"]))
            if m.get("gaps"):
                lines.append(f"- ⚠️ 리스크: " + " / ".join(m["gaps"]))
            # 알림문턱(기본 60) 넘는 유망 공고엔 지원·합격 전략 코멘트
            if m["score"] >= notify and m.get("strategy"):
                lines.append(f"- 🎯 **지원·합격 전략**")
                for s in m["strategy"]:
                    lines.append(f"    - {s}")
            lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path, len(shown)


# =========================================================================
# 5-b) HTML 대시보드 (자체 완결형, 외부 의존 0)
# =========================================================================
_DASH_TEMPLATE = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PERSON__ · 채용 플랫폼 매칭 대시보드</title>
<style>
  :root{color-scheme:light}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,'Segoe UI',Roboto,'Malgun Gothic',sans-serif;background:#f4f5f7;color:#1c2733}
  .wrap{max-width:1100px;margin:0 auto;padding:18px}
  h1{font-size:20px;margin:0 0 4px}
  .meta{color:#5b6b7b;font-size:13px;margin-bottom:12px}
  .ctrl{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
  .ctrl input{flex:1;min-width:160px;padding:8px 10px;border:1px solid #cdd6df;border-radius:8px;font-size:14px}
  .ctrl button{padding:8px 12px;border:1px solid #cdd6df;background:#fff;border-radius:8px;cursor:pointer;font-size:13px}
  .ctrl button.on{background:#1565c0;color:#fff;border-color:#1565c0}
  .tblwrap{overflow-x:auto;background:#fff;border:1px solid #e2e8ef;border-radius:10px}
  table{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}
  th,td{padding:9px 10px;text-align:left;border-bottom:1px solid #eef2f6;vertical-align:top}
  th{background:#fafbfc;color:#5b6b7b;font-weight:600;white-space:nowrap;position:sticky;top:0}
  tr.hot td{background:#fff8e6}
  tr.exp td{opacity:.5}
  .sc{font-weight:700;font-size:15px}
  .dl{white-space:nowrap;font-weight:600}
  .dl.urg{color:#c62828}
  .dl.roll{color:#2e7d32}
  .dl.unk{color:#98a5b3;font-weight:400}
  a.job{color:#1565c0;text-decoration:none;font-weight:600}
  a.job:hover{text-decoration:underline}
  .ol{color:#6b7887;font-size:12px;margin-top:3px;max-width:420px}
  .vd{white-space:nowrap;font-size:12px;color:#43536a}
  .foot{color:#98a5b3;font-size:12px;margin-top:12px;text-align:center}
  .empty{padding:30px;text-align:center;color:#98a5b3}
</style>
<div class="wrap">
  <h1>🎯 __PERSON__ · 채용 플랫폼 매칭 대시보드</h1>
  <div class="meta">생성 __GEN__ · 총 <b>__TOTAL__</b>건 · 🔥 __HOT__건 · ⏰ 마감임박 __URGENT__건 · 알림문턱 __NOTIFY__점</div>
  <div class="ctrl">
    <input id="q" placeholder="회사·공고 검색…">
    <button id="bScore" class="on">점수순</button>
    <button id="bDl">마감임박순</button>
  </div>
  <div class="tblwrap">
    <table>
      <thead><tr>
        <th>점수</th><th>마감</th><th>판정</th><th>회사</th><th>공고</th><th>경력</th><th>지역</th><th>등록</th>
      </tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div>
  <p class="foot">job-hunting-radar · 로컬 생성 · 외부 전송 없음</p>
</div>
<script>
const DATA = __DATA__;
let mode = 'score';
const q = document.getElementById('q');
function cell(txt){const td=document.createElement('td');td.textContent=txt;return td;}
function render(){
  const term=(q.value||'').trim().toLowerCase();
  let rows=DATA.filter(r=>!term||(r.company+' '+r.title).toLowerCase().includes(term));
  rows.sort((a,b)=> mode==='dl' ? (a.dsort-b.dsort)||(b.score-a.score) : (b.score-a.score)||(a.dsort-b.dsort));
  const tb=document.getElementById('tb');tb.innerHTML='';
  if(!rows.length){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=8;td.className='empty';td.textContent='조건에 맞는 공고가 없어요.';tr.appendChild(td);tb.appendChild(tr);return;}
  for(const r of rows){
    const tr=document.createElement('tr');
    if(r.expired)tr.className='exp';else if(r.hot)tr.className='hot';
    const sc=cell('');sc.innerHTML='<span class="sc">'+r.score+'</span>'+(r.hot?' 🔥':'');tr.appendChild(sc);
    const dl=document.createElement('td');const sp=document.createElement('span');
    sp.className='dl '+(r.urgent?'urg':(r.dstate==='rolling'?'roll':(r.dstate==='unknown'?'unk':'')));
    sp.textContent=(r.urgent?'⏰ ':'')+r.deadline;dl.appendChild(sp);tr.appendChild(dl);
    const vd=cell(r.verdict);vd.className='vd';tr.appendChild(vd);
    tr.appendChild(cell(r.company));
    const jc=document.createElement('td');const a=document.createElement('a');
    a.className='job';a.href=r.url;a.target='_blank';a.rel='noopener';a.textContent=r.title;jc.appendChild(a);
    if(r.one_liner){const d=document.createElement('div');d.className='ol';d.textContent=r.one_liner;jc.appendChild(d);}
    tr.appendChild(jc);
    tr.appendChild(cell(r.career));
    tr.appendChild(cell(r.region));
    tr.appendChild(cell(r.date));
    tb.appendChild(tr);
  }
}
document.getElementById('bScore').onclick=function(){mode='score';this.classList.add('on');document.getElementById('bDl').classList.remove('on');render();};
document.getElementById('bDl').onclick=function(){mode='dl';this.classList.add('on');document.getElementById('bScore').classList.remove('on');render();};
q.oninput=render;
render();
</script>
"""


def write_dashboard(cfg, seen):
    """seen의 매칭 카드들을 모아 자체 완결형 HTML 대시보드 생성(외부 의존 0).

    matches_dir/index.html 로 저장(매 실행 덮어씀). 카드는 notified 시 seen에 축적되므로
    여러 날치 매칭이 마감일·점수 기준으로 한눈에 정렬·검색된다. 카드 없으면 None.
    """
    import html as _html  # noqa: WPS433
    out_dir = os.path.join(HERE, cfg["output"]["matches_dir"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    keep = cfg["output"].get("seen_retention_days", 90)
    cutoff = (dt.date.today() - dt.timedelta(days=keep)).isoformat()
    notify = cfg["scoring"]["notify_threshold"]

    rows = []
    for rec in seen.values():
        c = rec.get("card")
        if not c or rec.get("first_seen", "9999") < cutoff:
            continue
        dlabel, ddays, dstate = deadline_info(c.get("deadline"))
        if dstate == "date":
            dsort = ddays if ddays is not None else 99999
        elif dstate == "rolling":
            dsort = 100000
        else:
            dsort = 100001
        rows.append({
            "score": c.get("score", 0),
            "hot": c.get("score", 0) >= notify,
            "verdict": c.get("verdict") or ("⚙️LLM미검증" if not c.get("llm") else "-"),
            "deadline": dlabel, "dstate": dstate, "dsort": dsort,
            "urgent": dstate == "date" and ddays is not None and 0 <= ddays <= 3,
            "expired": dstate == "date" and ddays is not None and ddays < 0,
            "company": c.get("company", "?"), "title": c.get("title", "?"),
            "url": c.get("url", "#"), "career": c.get("career", "-"),
            "region": c.get("region", "-"), "one_liner": c.get("one_liner") or "",
            "date": c.get("date", ""),
        })
    if not rows:
        return None
    rows.sort(key=lambda r: r["score"], reverse=True)
    hot = sum(1 for r in rows if r["hot"])
    urgent = sum(1 for r in rows if r["urgent"])
    data_json = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")

    doc = _DASH_TEMPLATE
    for k, v in {
        "__PERSON__": _html.escape(cfg.get("name", "") or "전체"),
        "__GEN__": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "__TOTAL__": str(len(rows)), "__HOT__": str(hot),
        "__URGENT__": str(urgent), "__NOTIFY__": str(notify),
        "__DATA__": data_json,
    }.items():
        doc = doc.replace(k, v)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


# =========================================================================
# 6) 알림 (텔레그램 푸시)
# =========================================================================
def send_telegram(cfg, matches, stats=None, note_path=None):
    """score ≥ notify.telegram.threshold 인 매칭이 있으면 **노트 md 파일을 첨부**해서 푸시.

    - 짧은 캡션(누구·건수·문턱·기간 + 상위 몇 건) + 노트 .md 파일 첨부(sendDocument).
      그룹에서 파일을 탭하면 노트 전체(총평·리스크·전략·공고 링크)를 앱에서 열람.
    - 봇 토큰은 **환경변수(bot_token_env, 기본 TELEGRAM_BOT_TOKEN)에서만** 읽는다.
    - chat_id는 config.notify.telegram.chat_id 또는 env TELEGRAM_CHAT_ID.
    - 설정/토큰/chat_id/대상 없으면 조용히 skip. 신규(재알림 안 된) 공고만이라 중복 없음.
    """
    tg = (cfg.get("notify") or {}).get("telegram") or {}
    if not tg.get("enabled"):
        return
    token = os.environ.get(tg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
    chat_id = tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("  · 텔레그램 skip: 봇 토큰(env) 또는 chat_id 없음")
        return
    threshold = tg.get("threshold", 50)
    hits = [m for m in matches if m["score"] >= threshold]
    if not hits:
        log(f"  · 텔레그램 skip: ≥{threshold}점 신규 없음")
        return
    hits.sort(key=lambda m: m["score"], reverse=True)
    notify = cfg["scoring"]["notify_threshold"]
    person = cfg.get("name", "")
    who = f"{person} · " if person else ""
    window = (stats or {}).get("window")
    span = f" · 최근 {window}일" if window is not None else ""
    lines = [f"🎯 {who}채용 플랫폼 매칭 {len(hits)}건 (≥{threshold}점{span})"]
    for m in hits[:8]:
        flag = "🔥" if m["score"] >= notify else "•"
        lines.append(f"{flag} {m['score']}점 · {m['company']} — {m['title']}")
    if len(hits) > 8:
        lines.append(f"…외 {len(hits) - 8}건")
    lines.append("📄 전체 상세·전략·공고링크는 첨부 노트 ↓")
    caption = "\n".join(lines)[:1024]

    # 노트 md 파일을 문서로 첨부. 파일 못 읽으면 캡션만 텍스트로 폴백.
    doc = None
    if note_path and os.path.isfile(note_path):
        try:
            with open(note_path, "rb") as f:
                doc = (os.path.basename(note_path), f.read(), "text/markdown")
        except Exception:  # noqa: BLE001
            doc = None
    try:
        if doc:
            res = http_post_multipart(
                f"https://api.telegram.org/bot{token}/sendDocument",
                {"chat_id": str(chat_id), "caption": caption},
                {"document": doc},
            )
        else:
            res = http_post_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat_id, "text": caption, "disable_web_page_preview": "true"},
            )
        if res.get("ok"):
            log(f"  ✓ 텔레그램 알림 전송 ({len(hits)}건 ≥{threshold}점"
                + (", 노트 첨부)" if doc else ")"))
        else:
            log(f"  ! 텔레그램 실패: {str(res)[:150]}")
    except Exception as e:  # noqa: BLE001
        log(f"  ! 텔레그램 전송 예외: {str(e)[:150]}")


def telegram_test(cfg):
    """--test-telegram: 봇 토큰(env)+chat_id 설정이 맞는지 테스트 메시지 1건 전송."""
    tg = (cfg.get("notify") or {}).get("telegram") or {}
    token = os.environ.get(tg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
    chat_id = tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        log("텔레그램 테스트 실패: 봇 토큰 환경변수 없음. "
            "setx 후 '새 터미널'에서 다시 실행하세요.")
        return
    if not chat_id:
        log("텔레그램 테스트 실패: config.notify.telegram.chat_id 가 비어있음.")
        return
    try:
        res = http_post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id,
             "text": "✅ job-hunting-radar 텔레그램 연결 OK. 이제 ≥50점 신규 매칭이 여기로 옵니다."},
        )
        if res.get("ok"):
            log(f"텔레그램 테스트 전송 성공 → chat_id {chat_id}. 앱에서 메시지 확인하세요.")
        else:
            log(f"텔레그램 테스트 실패: {str(res)[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"텔레그램 테스트 예외: {str(e)[:200]}")


# =========================================================================
# 메인
# =========================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="LLM 단계 생략(규칙 점수만)")
    ap.add_argument("--seed", action="store_true",
                    help="알림 없이 현재 스캔 공고를 notified로 기록(첫 세팅·소음 억제용)")
    ap.add_argument("--dry-run", action="store_true", help="노트/상태 저장 안 함")
    ap.add_argument("--config", default="config.json",
                    help="프로필 config 파일(다중 지원자용). 기본 config.json")
    ap.add_argument("--days", type=int, default=None, metavar="N",
                    help="신규로 볼 등록 경과일(createdAt 윈도우). "
                         "지정 시 config의 new_within_days를 덮어씀. "
                         "예: 매일 돌리면 --days 1, 3일만에 돌리면 --days 3")
    ap.add_argument("--test-telegram", action="store_true",
                    help="텔레그램 설정 확인용 테스트 메시지 1건 전송 후 종료")
    args = ap.parse_args()

    load_dotenv()  # .env(있으면)의 TELEGRAM_BOT_TOKEN 등 주입
    cfg = load_config(args.config)
    if args.test_telegram:
        telegram_test(cfg)
        return
    person = cfg.get("name", "")
    if person:
        log(f"===== 프로필: {person} ({args.config}) =====")
    profile_text = load_profile(cfg)
    seen = load_seen(cfg)  # {id: {first_seen, created_at, age?, notified, title, rule}}
    today = dt.date.today().isoformat()
    window = args.days if args.days is not None else cfg["search"]["new_within_days"]
    if args.days is not None:
        log(f"신규 윈도우: createdAt ≤ {window}일 (--days 오버라이드)")
    max_detail = cfg["search"]["max_detail_fetches"]

    log("공고 수집 시작…")
    listings = fetch_listings(cfg)
    log(f"총 {len(listings)}건 수집(dedupe)")

    stats = {
        "scanned": len(listings), "rule_pass": 0, "detail_fetched": 0,
        "fresh": 0, "llm_scored": 0, "enriched": 0, "llm_note": None,
        "window": window,
    }

    # 규칙 프리필터 → 상세조회 후보(점수순)
    prelim = []
    for it in listings:
        rid = it["id"]
        seen.setdefault(rid, {"first_seen": today, "title": it.get("title")})
        if title_excluded(it, cfg["filter"]):
            continue
        rsc, parts = rule_score(it, cfg)
        if rsc < cfg["scoring"]["rule_threshold"] or parts["role"] == 0.0:
            continue
        stats["rule_pass"] += 1
        prelim.append((it, rsc))
    prelim.sort(key=lambda x: x[1], reverse=True)
    log(f"규칙 통과 {stats['rule_pass']}건 → createdAt 확인")

    # createdAt으로 '진짜 신규' 판정. 상세는 id당 1회만(캐시). detail 재사용.
    fresh = []  # (it, rsc, detail, age)
    for it, rsc in prelim:
        rid = it["id"]
        rec = seen[rid]
        if rec.get("notified"):
            continue  # 이미 알린 공고 재알림 안 함
        detail = None
        age = created_age_days(rec.get("created_at"))
        if age is None:  # createdAt 아직 모름 → 상세 1회 조회
            if stats["detail_fetched"] >= max_detail:
                continue  # 이번 실행 상세 예산 소진(다음 실행에서 확인)
            try:
                detail = fetch_detail(rid)
                stats["detail_fetched"] += 1
            except RuntimeError as e:
                log(f"  ! 상세 실패({rid[:8]}): {e}")
                continue
            rec["created_at"] = detail.get("createdAt")
            age = created_age_days(rec.get("created_at"))
        if age is not None and age <= window:
            stats["fresh"] += 1
            fresh.append((it, rsc, detail, age))
    log(f"신규(createdAt≤{window}일) {stats['fresh']}건")

    if args.seed:
        for it, rsc, _d, _a in fresh:
            seen[it["id"]]["notified"] = True
        if not args.dry_run:
            save_seen(cfg, seen)
        log(f"--seed 완료: 신규 {stats['fresh']}건을 notified 처리. "
            f"(총 {len(seen)} id 기록) 다음 실행부터 새 공고만 알림.")
        return

    # LLM 정밀 점수
    engine, why = resolve_engine(cfg, args.no_llm)
    if engine is None:
        stats["llm_note"] = why
        log(f"LLM 생략: {why}")
    else:
        log(f"LLM 엔진: {engine['provider']} ({engine['model']})")

    fresh.sort(key=lambda x: x[1], reverse=True)
    llm_budget = cfg["llm"]["max_calls_per_run"]
    to_score = fresh[:llm_budget] if engine is not None else []
    rest = fresh[llm_budget:] if engine is not None else fresh

    # LLM 대상의 상세 확보 + base JD. 빈약하면 enrich 후보로.
    enr_cfg = cfg.get("enrich") or {}
    min_chars = enr_cfg.get("min_chars", 100)
    prepared = []  # (it, rsc, detail, age, base_text, is_thin)
    to_crawl = {}
    for it, rsc, detail, age in to_score:
        if detail is None:
            detail = fetch_detail(it["id"])
        base = detail_to_text(detail)
        is_thin = len(base) < min_chars
        if (enr_cfg.get("enabled") and is_thin and detail.get("redirectUrl")
                and len(to_crawl) < enr_cfg.get("max_crawls_per_run", 15)):
            to_crawl[it["id"]] = detail["redirectUrl"]
        prepared.append((it, rsc, detail, age, base, is_thin))

    enriched = {}
    if to_crawl:
        log(f"원본 크롤 시작(crawl4ai): 빈약 공고 {len(to_crawl)}건…")
        enriched = crawl_originals(to_crawl, enr_cfg.get("page_timeout_sec", 45))
        stats["enriched"] = len(enriched)
        log(f"원본 확보 {len(enriched)}/{len(to_crawl)}건")

    matches = []
    for it, rsc, detail, age, base, is_thin in prepared:
        rid = it["id"]
        jd = enriched.get(rid) or base
        jd_source = ("원본크롤" if rid in enriched
                     else ("채용 플랫폼-빈약" if is_thin else "채용 플랫폼"))
        llm_out = None
        try:
            llm_out = llm_score(
                engine, cfg["llm"]["max_output_tokens"], profile_text, it, jd)
            stats["llm_scored"] += 1
        except Exception as e:  # noqa: BLE001
            log(f"  ! LLM 실패({rid[:8]}): {e}")
        matches.append(_build_match(it, rsc, detail, age, llm_out, jd_source))

    # 예산 초과분: 규칙 점수만
    for it, rsc, detail, age in rest:
        matches.append(_build_match(it, rsc, detail, age, None,
                                    "채용 플랫폼" if detail else None))

    # 콘솔 요약
    matches.sort(key=lambda m: m["score"], reverse=True)
    notify = cfg["scoring"]["notify_threshold"]
    log(f"=== 신규 매칭 (알림문턱 {notify}) ===")
    for m in matches[:12]:
        flag = "🔥" if m["score"] >= notify else "  "
        log(f"  {flag} {m['score']:>5} | {m['company']} | {m['title'][:34]}")

    if args.dry_run:
        log("--dry-run: 노트/상태 저장 생략")
        return

    # 노트에 수록된 공고만 notified 처리 + 대시보드용 카드 축적
    minsc = cfg["output"]["min_score_in_note"]
    for m in matches:
        if m["score"] >= minsc:
            rec = seen[m["id"]]
            rec["notified"] = True
            rec["rule"] = m["rule"]
            rec["card"] = {
                "score": m["score"], "company": m["company"], "title": m["title"],
                "url": m["url"], "verdict": m.get("verdict"),
                "deadline": m.get("deadline"), "one_liner": m.get("one_liner"),
                "career": m["career"], "region": m["region"],
                "llm": m.get("llm") is not None, "date": today,
            }

    path, n = write_note(cfg, matches, stats)
    save_seen(cfg, seen)
    dash = write_dashboard(cfg, seen)
    send_telegram(cfg, matches, stats, path)
    hot = sum(1 for m in matches if m["score"] >= notify)
    log(f"노트 작성: {path} (수록 {n}건, 🔥{hot}건)")
    if dash:
        log(f"대시보드: {dash}")


if __name__ == "__main__":
    main()
