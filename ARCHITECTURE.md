# 파이프라인 구조도

한 프로필 1회 실행의 흐름. `run.bat`이 프로필마다 이 흐름을 순서대로 돈다.

```mermaid
flowchart TD
    CFG["⚙️ config.{name}.json<br/>채용 플랫폼 필터 조건·가중치·문턱"]:::cfg
    PROF["📄 profile.{name}.md<br/>이력 요약(LLM 입력)"]:::cfg
    RUN["⏰ run.bat<br/>작업 스케줄러 (하루 N회)"]:::sched

    RUN --> S1
    CFG --> S1

    S1["① 수집 · fetch_listings<br/>채용 플랫폼 필터를 서버단 쿼리로 변환"]:::step
    APIL[("채용 플랫폼 공개 API<br/>api.example.com/recruitments")]:::api
    S1 -->|"depthTwos·regions·career·<br/>employeeTypes·educations·companyTypes"| APIL
    APIL -->|"필터 유니버스 전체<br/>페이지네이션 dedupe"| S2

    S2["② 규칙 프리필터 + 점수 · rule_score<br/>직무·경력·기술·지역·고용형태 가중합"]:::step
    S2 -->|"rule≥45 & 역할점수≠0<br/>(제외키워드 컷)"| S3

    S3["③ 신규 판정 · createdAt ≤ N일<br/>페이지 드리프트 무관, 등록일로 판정"]:::step
    APID[("상세 API<br/>createdAt·JD 본문·지원링크")]:::api
    S3 -->|"id당 1회만 조회 후 캐시"| APID
    S3 -->|"이미 notified면 제외<br/>= 진짜 신규만"| SE

    SE["③-b enrich · crawl_originals<br/>채용 플랫폼 본문 빈약(&lt;min_chars)이면<br/>원본을 crawl4ai로 크롤"]:::step
    CR[["🕷️ crawl4ai (로컬 헤드리스)<br/>기업 자체 SPA 렌더링 → JD 마크다운"]]:::llm
    SE <-->|"redirectUrl 원본 JD<br/>실패 시 폴백(⚠️ 표시)"| CR
    SE --> S4

    S4["④ LLM 정밀 채점 · llm_score<br/>이력 ↔ (원본 or 채용 플랫폼) JD 대조"]:::step
    PROF --> S4
    LLM[["🤖 Claude (sonnet)<br/>claude -p 헤드리스 = 구독 채점"]]:::llm
    S4 <-->|"score·verdict·근거·리스크·전략"| LLM
    S4 --> S5

    S5["⑤ 출력 · write_note + save_seen"]:::step
    NOTE["📝 matches/{name}/YYYY-MM-DD.md<br/>점수표 + 근거/리스크<br/>🔥 문턱↑: 지원·합격 전략"]:::out
    STATE[("💾 state/{name}.json<br/>createdAt 캐시 · notified")]:::store
    S5 -->|"min_score_in_note↑ 수록"| NOTE
    S5 -->|"본 공고 기록"| STATE
    STATE -.->|"재알림 방지 피드백"| S3

    classDef step fill:#e3f2fd,stroke:#1565c0,color:#0d47a1;
    classDef api fill:#fff3e0,stroke:#e65100,color:#e65100;
    classDef llm fill:#f3e5f5,stroke:#6a1b9a,color:#4a148c;
    classDef out fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    classDef store fill:#eceff1,stroke:#455a64,color:#263238;
    classDef cfg fill:#fffde7,stroke:#f9a825,color:#f57f17;
    classDef sched fill:#fce4ec,stroke:#c2185b,color:#880e4f;
```

## 단계별 요약

| 단계 | 하는 일 | 핵심 포인트 |
|---|---|---|
| ① 수집 | 채용 플랫폼 필터 = API 파라미터로 변환해 유니버스 전체를 긁음 | 사이트에서 건 필터와 **동일 집합**. 소량이라 통째로 수집 |
| ② 규칙 점수 | 직무/경력/기술/지역/고용형태 가중합 | 값싼 프리필터. 문턱 미달·비대상 컷 |
| ③ 신규 판정 | 상세의 `createdAt`이 N일 이내만 | 페이지네이션 불안정 무관. 상세는 **id당 1회** 조회·캐시 |
| ③-b enrich | 채용 플랫폼 본문 빈약하면 원본을 crawl4ai로 크롤 | 기업 자체페이지 JD 확보. 실패 시 폴백 |
| ④ LLM 채점 | 이력 ↔ (원본/채용 플랫폼) JD 정밀 대조 | 구독(`claude -p`) 또는 API로 채점. 근거·리스크·**전략** 생성 |
| ⑤ 출력 | 노트 작성 + 상태 저장 | 문턱↑ 🔥+전략, notified로 재알림 방지 |

## 데이터 상태 (state/{name}.json)

```
{ "<공고id>": { "first_seen": "날짜", "created_at": "채용 플랫폼 등록시각",
                "notified": true, "rule": 80.0, "title": "..." } }
```

- `created_at`: 상세 재조회 안 하려는 캐시. `notified`: 재알림 방지.
- 필터·프로필을 크게 바꾸면 이 파일을 지우고 재실행 = 깨끗한 첫 다이제스트.

## 왜 이렇게(하이브리드 + createdAt) 설계했나

- **채용 플랫폼 API가 안정적 최신순 목록을 안 줌** → 단순 diff는 가짜 신규 폭증 → `createdAt`으로 판정.
- **규칙만으론 변별력 부족**(제목·카테고리만 봄) → 신규 소수만 LLM이 JD까지 읽어 정밀 채점.
- **비용 통제**: 값싼 규칙으로 좁히고, 진짜 신규(하루 몇 건)만 LLM. 상세도 신규만 조회.
