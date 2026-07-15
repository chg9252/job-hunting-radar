# job-hunting-radar

채용 플랫폼에 올라오는 채용 공고를 내 이력에 대입해,
**가능성 높은 신규 공고**만 골라 마크다운 다이제스트로 알려주는 로컬 자동화 도구.

규칙 기반 프리필터로 후보를 좁히고, 진짜 신규 공고만 LLM(Claude)이 이력서와 정밀 대조해
점수·근거·리스크·지원전략을 채운다. Obsidian 등 어떤 마크다운 뷰어로도 결과를 볼 수 있다.

> ⚠️ 이 리포엔 **예시 템플릿**(`config.example.json`, `profile.example.md`, `.env.example`)만 들어있다.
> 실제 설정·이력·이력서·매칭 결과는 `.gitignore`로 제외된다(개인정보 보호).
> **크롤 대상(플랫폼명·API 베이스)은 코드에 없고 `.env`로만 주입한다**(`.env.example`의 `PLATFORM_*` 참고).

## 동작 원리

```
채용 플랫폼 공개 API(베이스는 .env) 수집
  → 규칙 프리필터(직무·경력·지역·제외키워드) + 가중치 점수
  → createdAt(플랫폼 등록일)로 "진짜 신규"만 선별 (기본 7일 이내)
  → (빈약한 공고는 원본 채용페이지를 crawl4ai로 크롤해 실제 JD 확보)
  → 상위 신규만 Claude가 이력서와 정밀 대조해 재점수 + 근거/리스크/전략
  → matches/ 마크다운 노트로 다이제스트 출력, 본 공고는 notified 기록(재알림 방지)
```

- **하이브리드**: 규칙으로 수백 건 중 후보를 좁히고, 진짜 신규(하루 몇 건)만 LLM 채점 → 비용 소액.
- **LLM 없이도 동작**: 키/구독이 없으면 규칙 점수만으로 노트 생성(자동 폴백).

전체 파이프라인 그림은 [ARCHITECTURE.md](ARCHITECTURE.md) 참고.

## 왜 createdAt으로 신규를 판정하나

플랫폼 목록 API는 **안정적인 최신순 완전 목록을 주지 않는다**(offset 페이지네이션이 실행마다
드리프트해 매번 다른 슬라이스 반환). "이전에 못 본 id = 신규"로 단순 diff하면 가짜 신규가 쏟아진다.
대신 **상세의 `createdAt`(플랫폼이 그 공고를 처음 등록한 시각)**을 신규 판정 기준으로 쓴다.
페이지네이션이 흔들려도 판정은 흔들리지 않는다. 상세는 id당 한 번만 조회해 `state/`에 캐시한다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `job_watcher.py` | 본체 (수집·점수·신규판정·enrich·LLM·노트) |
| `.env.example` | 크롤 대상(`PLATFORM_*`)·토큰 템플릿. `.env`로 복사해 사용 (실제 파일은 gitignore) |
| `config.example.json` | 설정 템플릿. `config.json`으로 복사해 사용 (실제 파일은 gitignore) |
| `profile.example.md` | 이력 요약 템플릿. `profile.md`로 복사해 사용 |
| `run.bat` | 스케줄러용 실행 래퍼 (모든 프로필 순차 실행) |
| `requirements.txt` | 선택 의존성 (`anthropic`, `crawl4ai`) |
| `matches/{name}/YYYY-MM-DD.md` | (자동생성) 그날의 매칭 다이제스트 |
| `state/{name}.json` | (자동생성) 본 공고 id·createdAt 캐시·notified 플래그 |

## 빠른 시작

```bash
# 1) 크롤 대상·설정·프로필 준비 (예시를 복사해서 본인 것으로 수정)
cp .env.example .env            # PLATFORM_NAME / PLATFORM_API_BASE / PLATFORM_SITE_BASE 채우기
cp config.example.json config.json
cp profile.example.md  profile.md

# 2) 동작 확인 (LLM·저장 없이 콘솔 출력만)
python job_watcher.py --no-llm --dry-run

# 3) 정식 실행 → matches/ 아래에 오늘 날짜 노트 생성
python job_watcher.py
```

> `PLATFORM_API_BASE`가 비어 있으면 수집 단계에서 안내 후 종료한다(크롤 대상은 코드에 없기 때문).

### 설치 (crawl4ai enrich를 쓸 경우, 전용 venv 권장)

`crawl4ai`가 무거운 의존성(numpy 등)을 끌고 오므로 전역 파이썬과 얽히지 않게
**전용 가상환경 `.venv`** 에 격리한다. `run.bat`은 `.venv` 파이썬을 자동으로 쓴다(없으면 전역 폴백).

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt   # anthropic(선택), crawl4ai(선택)
.\.venv\Scripts\crawl4ai-setup                              # Chromium 1회 설치(headless)
```

- crawl4ai/anthropic 없이 규칙 점수만 쓸 거면 venv 없이 전역 `python`으로도 동작한다.

## 실행 옵션

```bash
python job_watcher.py             # 전체 실행(규칙→enrich→LLM→노트). 신규 윈도우 config 기본(7일)
python job_watcher.py --days 1    # 매일 돌릴 때: 하루 내 신규만
python job_watcher.py --days 3    # 3일 만에 돌릴 때: 3일 내 신규
python job_watcher.py --no-llm    # 규칙 점수만 (키·구독 없이 테스트)
python job_watcher.py --dry-run   # 저장 안 하고 콘솔 출력만
python job_watcher.py --seed      # 알림 없이 현재 백로그를 notified 처리
python job_watcher.py --config config.other.json   # 다른 프로필로 실행
```

**최초 세팅 권장 순서**: ① `--no-llm --dry-run`으로 매칭 확인 → ② (선택) `--seed`를 2~3번
실행해 기존 공고를 조용히 소진(이후 새 공고만 알림) → ③ 정식 실행.

## LLM(하이브리드) 활성화

규칙만으로도 필터·정렬은 되지만, 정밀 매칭·근거·리스크·전략은 LLM이 채운다.
`config.json`의 `llm.provider`로 백엔드를 고른다. **아래 둘 중 하나만 있으면 됨.**

### 방식 A: Claude Code 구독 (별도 키·과금 없음)

```json
"llm": { "provider": "claude_cli", "cli_model": "sonnet" }
```

- `claude` CLI를 헤드리스(`claude -p`)로 호출해 채점. **API 키 불필요.** 구독 사용량으로 처리.
- 주의: 호출마다 CLI 하네스 컨텍스트(~30k 토큰)가 얹혀 API 직접호출보다 토큰 부하가 큼.
  하루 십수 건 규모라 무리 없음. 더 빠르고 싸게 하려면 `cli_model`을 `"haiku"`로.

### 방식 B: Anthropic API 키 (직접 과금, 부하 작음)

```bash
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."   # Windows: setx ANTHROPIC_API_KEY "..."
```
```json
"llm": { "provider": "api", "model": "claude-sonnet-5" }
```

- 키는 <https://console.anthropic.com> 발급(구독과 별개, 사용량 과금). 호출당 ~600토큰이라 소액.

### 공통

- `llm.max_calls_per_run`이 1회 실행 채점 상한. 초과분은 규칙 점수만 매겨 노트에 `⚙️ LLM 미검증` 표시(LLM이 안 본 공고 = 규칙 점수만, 신뢰도 낮음).
- 어느 방식도 준비 안 되면 노트 상단에 "LLM 생략" 배너가 뜨고 규칙 점수로만 채운다.

## 원본 JD 크롤 (enrich)

일부 공고는 플랫폼이 **JD 전문을 저장**하지 않고 기업 자체 채용페이지로 보내기만 한다.
그런 공고는 제목+태그 2~3개만 갖고 있어(대기업 필터 기준 약 96%가 이런 "빈약" 공고),
그대로면 LLM이 제목·직무·경력만 보고 추정한다. 이를 보완하려고 **빈약한 공고는 원본 링크를
crawl4ai(로컬 헤드리스 브라우저)로 크롤해 실제 JD를 LLM에 전달**한다.

- 미설치면 enrich 자동 생략, 빈약 본문으로 폴백(노트에 `⚠️ 직접 확인`).
- 동작: 신규 + LLM 대상 중 플랫폼 본문이 `enrich.min_chars` 미만이고 원본 링크가 있으면 크롤.
- 무료·로컬: API 키·크레딧 없음. 성공 시 노트에 `🔎 원본 JD 크롤 반영`.

```json
"enrich": { "enabled": true, "min_chars": 1000, "max_crawls_per_run": 20, "page_timeout_sec": 45 }
```

## 텔레그램 알림 (점수 ≥ threshold 신규 매칭 푸시)

노트 외에, **일정 점수 이상 신규 매칭이 뜨면 텔레그램으로 즉시 푸시**할 수 있다.
`matches`는 그 실행에서 처음 잡힌(재알림 안 된) 공고만이라 **중복 알림이 없다.**

**1회 세팅**:

1. 텔레그램에서 **@BotFather** 와 대화 → `/newbot` → 봇 이름·username 정하면 **봇 토큰** 발급.
2. 방금 만든 내 봇을 검색해 대화방을 열고 아무 메시지나 한 번 보낸다(봇이 나를 알게 하려고).
3. **chat_id** 얻기: 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 열면
   `"chat":{"id":123456789}` 숫자가 내 chat_id. (또는 텔레그램에서 **@userinfobot** 에게 물어봄)
4. 봇 토큰은 **비밀**이라 코드·config에 넣지 않고 `.env` 파일이나 환경변수로 준다.
   `.env`가 가장 간단하다(`.gitignore`되어 커밋 안 됨):
   ```bash
   cp .env.example .env
   # .env 를 열어 TELEGRAM_BOT_TOKEN=123456:ABC-... 한 줄 채우기
   ```
   (또는 환경변수로: `setx TELEGRAM_BOT_TOKEN "123456:ABC-..."` — 이 경우 새 콘솔부터 적용)
5. `config.json`의 `notify.telegram`에 `chat_id`와 문턱 설정:
   ```json
   "notify": { "telegram": { "enabled": true, "threshold": 50, "chat_id": "123456789", "bot_token_env": "TELEGRAM_BOT_TOKEN" } }
   ```
6. 확인: `python job_watcher.py --test-telegram` → 텔레그램에 테스트 메시지가 오면 완료.

- `threshold`: 이 점수 **이상** 신규 매칭이 있을 때만 푸시(기본 50).
- 토큰이나 chat_id가 없으면 조용히 skip(노트는 정상 생성).
- 토큰 로딩 우선순위: OS 환경변수 → `.env` 파일(둘 다 있으면 환경변수 우선).
- 여러 프로필이 서로 다른 사람에게 가야 하면 config마다 `chat_id`를 다르게 준다(봇 토큰은 공용).

## 서류 마감일 + HTML 대시보드

**마감일**: 플랫폼 API가 마감일을 주지 않는 경우가 많다(대부분 "상시채용"). 대신 크롤한 원본 JD에서
**LLM이 마감일을 추출**한다(추가 호출 없음). 결과는 `D-5 (07/31)` / `상시` / `미상` 형태로
노트·대시보드에 표시되고, **D-3 이내는 ⏰ 마감임박**으로 강조된다.

**HTML 대시보드**: 매 실행마다 `{matches_dir}/index.html` 을 생성한다(자체 완결형, 외부 의존 0).

- notified된 매칭이 `state`에 카드로 쌓여, **여러 날치 매칭을 한 페이지에서** 점수·마감일로 정렬·검색.
- 마감임박순/점수순 토글, 회사·공고 검색, 🔥강조, 마감 지남은 흐리게 표시, 제목 클릭 시 공고로 이동.
- 브라우저로 열기만 하면 됨. `matches_dir`이 구글드라이브면 동기화돼 여러 기기에서 열람.
- 개인정보라 어디에도 전송하지 않고 로컬 파일로만 생성된다.

## 매일 자동 실행 (Windows 작업 스케줄러)

```powershell
schtasks /Create /TN "job-hunting-radar" /SC DAILY /ST 09:00 ^
  /TR "\"<이 폴더 경로>\run.bat\"" /F
```

- 확인: `schtasks /Query /TN "job-hunting-radar"` · 즉시 실행: `schtasks /Run /TN "job-hunting-radar"`
- PC가 켜져 있을 때만 동작(로컬 방식). 놓친 실행은 다음 실행이 신규 윈도우로 커버.
- macOS/Linux는 cron으로 `run` 스크립트를 걸면 된다.

## 다중 지원자(프로필)

여러 사람 이력을 각각 매칭할 수 있다. 사람마다 **config + profile + state + matches**를 분리한다.

- `config.{누구}.json` 1개를 만들고(예시 복사) 그 안에서 `name`, 필터, `output`의
  `profile_file`/`state_file`/`matches_dir`을 사람별로 지정.
- 실행: `python job_watcher.py --config config.{누구}.json`.
- `run.bat`에 프로필별로 한 줄씩 추가하면 스케줄러 한 번으로 전부 돈다.
- 같은 공고도 각자 이력에 맞춰 다르게 채점된다.

## 주요 config 노브

| 키 | 의미 |
|---|---|
| `search.filters` | 채용 플랫폼 사이트 필터와 동일(직무·지역·경력·고용형태·학력·기업규모). 값은 플랫폼 표기 그대로 |
| `search.new_within_days` | 신규로 볼 등록 경과일 기본값(기본 7). 실행 시 `--days N`으로 덮어씀 |
| `filter.exclude_title_keywords` | 제목에 들어가면 제외(프론트·모바일·QA 등) |
| `profile.career_years` | 연차. 경력 매칭 기준 |
| `profile.tech_primary/secondary` | 제목 가점 기술 키워드(소문자) |
| `scoring.rule_threshold` | 이 규칙점수 미만은 LLM·노트 제외(기본 45) |
| `scoring.notify_threshold` | 🔥 강조 + 지원·합격 전략 코멘트 문턱(기본 60) |
| `output.min_score_in_note` | 노트에 실을 최소 점수 |

## 한계·주의

- `createdAt`은 "플랫폼이 그 공고를 처음 수집한 시각"이라 원 사이트 게시일과 며칠 다를 수 있다(신규 알림엔 충분).
- 플랫폼의 **공개 API 응답 스키마**에 의존한다. 스키마가 바뀌면 `job_watcher.py`의 API 상수·필드명을 손봐야 한다.
- crawl4ai 크롤은 일부 사이트에서 빈 결과가 올 수 있고, 그때는 폴백 표시(`⚠️`)된다.
- 알림 채널은 현재 **마크다운 노트 + 텔레그램**. 카카오 등은 추후 추가 가능.

## 라이선스

MIT
