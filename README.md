# 청년 법률 상담 AI

어려운 법률 용어를 몰라도 질문에 맞는 법령을 찾도록 만든 팀 프로젝트입니다. 개인 후속 작업에서는 **첫 금융 상담에 필요한 자료·질문을 정리하는 카드**를 추가하고, 과거 백업을 복원해도 철회한 기록이 다시 조회되지 않도록 복구 절차를 구성했습니다.

## 맡은 범위

- **팀 프로젝트** · 금융 분야 Agent와 질문 분류, 법령 청킹·적재, BM25와 임베딩을 결합한 검색 구현.
- **개인 후속 개발** · 상담 준비 카드, 인증·철회·만료 삭제, 답변 지침 비교와 AWS 백업·권한 복구 검증.

## 구조 요약

```text
팀 RAG: 질문 분류 → 금융 Agent → 법령 검색 → 근거를 포함한 답변
개인 앱: 상담 준비 카드 → SQLite 기록 → S3 백업
복구: 백업의 격리 사본 → DynamoDB 최신 삭제 이력 적용 → 검사 후 연결
```

**검색·실행 환경** · BM25·Chroma/pgvector, Amazon Bedrock / 개인 리허설 앱은 EC2·SQLite. 답변 평가는 실행 경로와 별도로 고정 문항·검색 결과·모델·채점 조건을 맞춰 비교했습니다.

## 확인한 결과

- **금융 검색 hit@3 0.975 · MRR@3 0.840** · 개인 재평가의 개발용 120문항 중 117문항에서 상위 3개에 정답 조문 포함. [검색 평가 기록](https://jungseongheon.org/portfolio/evidence/index.html#youth-evaluation)
- **RAGAS 근거 충실도 `0.783 → 0.903`** · 같은 가상 금융 질문 12개로 답변 지침 비교. 점수 상승과 함께 필수 조항 누락을 확인해 기본 지침 유지. [평가 조건·결과](https://jungseongheon.org/portfolio/evidence/finance-ragas-comparison-20261019.json)
- **새 서버 복구 234.52초** · AWS 합성 자료 복구 1회에서 보존 대상 6건의 내용 해시 일치, 철회 기록 제외 확인. [복구 기록](docs/engineering/aws-rehearsal-decisions.md)
- **상태 API `200 → 503 → 200`** · 별도 HTTP IAM 차단·복구 시험. 연결 풀 수동 갱신과 복구 처리 후 응답 전환 확인. [HTTP 검증 기록](docs/engineering/http-iam-revalidation-20261021.md)

[포트폴리오 상세](https://jungseongheon.org/portfolio/#youth-law) · [상담 준비·기록 복구 구현](docs/finance-consultation-recovery.md) · [실행 방법](#실행)

## 후속 개발 검증 범위

[개발자 리허설 앱](app/rehearsal_app.py) · [삭제 이력과 복구 코드](app/participant_recovery.py)

상담 흐름의 현재 범위는 로컬 더미 검증입니다. 별도로 수행한 AWS 합성 데이터 리허설은 완료 후 자원을 정리했으며, 상시 서비스 검증은 아닙니다. 전문가 검토는 대기 중이고 실제 참가자 접수는 닫혀 있습니다. 팀 작업·개인 후속 개발, 모델 평가·사용자 효과·운영 검사를 구분합니다.

개인 후속 개발에서 수행한 AWS 합성 데이터 리허설은 [실행 경계와 종료 절차](docs/aws-rehearsal.md), [초기 리허설 결정과 관찰 한계](docs/engineering/aws-rehearsal-decisions.md), [초기 리허설 비식별 결과](docs/evidence/aws-rehearsal-20261021.json)에 기록했습니다. 별도의 IAM 연결 재검증은 [기존 연결에 대한 후속 재검증](docs/engineering/iam-connection-revalidation-20261021.md), [실제 HTTP 경로로 수행한 추가 차단·복구 시험](docs/engineering/http-iam-revalidation-20261021.md), [비식별 증거](docs/evidence/http-iam-revalidation-20261021.json)에 정리했습니다. 모든 후속 검증은 팀 프로젝트와 구분되는 개인 작업이며, 실제 사용자·전문가 검토나 운영 서비스 검증을 뜻하지 않습니다. [공개 포트폴리오](https://jungseongheon.org/portfolio)

<a id="차별점-범용-ai-대비--구조적으로-못-하는-것"></a>

## 답변과 근거 제공 방식
1. **법령 근거**: 수집한 국가법령정보센터 조문의 원문·출처·시행일을 답변에 표시하고 인용 여부 점검
2. **문의 경로**: 코드에 등록한 분야별 공식 연락처·링크 사용 (LLM 생성 금지, contacts.py)
3. **문서 초안 생성**: 내용증명·진정서 초안을 법령 근거 담아 생성 (drafter.py, 빈칸은 사용자가 채움)

## 4분야 자동 라우팅
입력창 하나. 사용자는 분야를 안 고름 → Supervisor가 자동 분류.
- 1개 분야 → 해당 분야 Agent / 복수 분야 → Agent 동시 실행(fan-out) → Planner 종합
- 범위 밖(형사 등) → 정직하게 거절(out_of_scope)

## 구조
```
docs/TEAM_GUIDE.md         # 협업 시작 절차 (필독)
docs/API_SETUP.md          # API 신청 절차
.github/workflows/ci.yml  # 계약 테스트 CI
state.py              # 공유 State 스키마 (SPEC §2). 공용 ⚠️
graph.py              # 그래프 조립 (fan-out/fan-in). 공용 ⚠️
common/rag.py         # 공통 RAG 헬퍼 (검색 + 고도화 지점). 공용 ⚠️
common/contacts.py    # 사전 등록한 공식 연락처·링크. 공용 ⚠️
common/drafter.py     # 문서 초안 생성. 공용 ⚠️
agents/supervisor.py  # 분야 자동 분류·라우팅. 공용 ⚠️
agents/verifier.py    # 답변의 인용·근거 점검. 공용 ⚠️
agents/planner.py     # 검증 결과를 반영한 종합·출처·연락처 구성. 공용 ⚠️
common/llm.py         # Bedrock 호출 + 구조화 출력 강제. 공용 ⚠️
agents/{labor,housing,consumer,finance}.py  # 담당 A/B/C/D — 각자 RAG + 문서초안
app/                  # 웹서비스 — FastAPI(api.py)+Jinja 메인화면(templates/)+Streamlit(ui_streamlit.py)
pipeline/             # medallion 파이프라인 (bronze수집→silver청킹→gold적재) + 증분감지
airflow/dags/         # 주1회 증분 갱신 DAG (EC2 Airflow)
scripts/build_index.py  # 최초 전체 구축 (pipeline 위임)
scripts/update_laws.py  # 증분 배치 진입점 (cron/Airflow 호출)
scripts/evaluate.py     # 3축(평가/비용/환각) 스코어카드 + 이력 누적
evals/<분야>.jsonl      # 분야별 평가셋 (각자 10~20문항으로 확장)
common/cost.py          # 모델 티어링 + 토큰·비용 추적. 공용 ⚠️
tests/test_contracts.py # 계약 테스트
```

## 문서 안내
- **docs/ONBOARDING.md** — ★처음 시작★ 환경 셋업·데이터 빌드·확인·작업 시작 절차 (새 팀원 여기부터)
- **docs/DATA_FLOW.md** — 데이터 흐름·형태 (메달리온: bronze→silver→gold→검색, 실제 데이터 예시)
- **docs/HARNESS.md** — verifier·contacts·llm·CI의 답변·출처·형식 점검 구조
- **docs/TEAM_GUIDE.md** — 팀 협업 시작 절차 (Day0 킥오프 → 일일 리듬 → 체크포인트). **팀원 전원 필독**
- **docs/API_SETUP.md** — 데이터 API 인증키 신청 절차 (Day 0)
- docs/SPEC.md — State·I/O 계약 (코드보다 우선) / CLAUDE.md — 작업 규약 / docs/TODO.md — 작업 보드
- docs/HANDOFF.md — 새 채팅에 맥락 이어주는 인수인계
- .github/workflows/ci.yml — PR과 main/dev push마다 계약 테스트 자동 실행

## 실행
```bash
cp .env.example .env               # 키 채우기 (팀 공유, docs/API_SETUP.md)
pip install -r requirements.txt
python graph.py                          # 단일/복수/범위밖 3케이스 확인 (CLI, 데이터 없어도 stub로 동작)
python -m pytest tests/                  # 테스트 실행 (실행 결과에서 검사 수 확인)
uvicorn app.api:app --reload --port 8000 # 웹서비스 (메인화면 http://localhost:8000, API문서 /docs)
streamlit run app/ui_streamlit.py        # 데모 UI (발표용, http://localhost:8501)
ruff format . && ruff check .            # 스타일 통일
```

### 실데이터로 RAG 켜기 (stub → 실검색)
```bash
pip install chromadb sentence-transformers rank-bm25 kiwipiepy   # build/검색에 필요
# .env에 LAW_GO_KR_OC 채운 뒤 (벡터DB는 이 키 하나로 구축됨)
python scripts/build_index.py all            # 4분야 구축 (bronze→silver→gold)
```
> ⚠️ **Windows**: 콘솔이 cp949라 스크립트의 `✓` 출력에서 깨질 수 있음 → `set PYTHONUTF8=1`
> (PowerShell은 `$env:PYTHONUTF8=1`) 후 실행. 첫 실행 시 임베딩 모델 ~400MB 다운로드.

### 협업 규약 (CLAUDE.md '브랜치 전략'·'커밋 규칙')
- `main` 직접 push 금지. 평소 통합 브랜치는 `dev`, 릴리스 때만 `dev` → `main` PR.
- 변경 **5개 미만 & 공용 파일 미포함** → `dev` 직접 push 허용 (push 전 로컬 `pytest` 통과 필수).
  **5개 이상** 또는 **공용 파일**(state/graph/rag/contacts/drafter/supervisor/planner/llm/cost/pipeline) → `feat/*` 브랜치 + `dev` PR.
- 커밋: `<type>(<scope>): 한글 제목` (Conventional Commits). 브랜치: `<type>/<scope>-주제`.
- 개인 메모는 `cp CLAUDE.local.md.example CLAUDE.local.md` (커밋 안 됨).

## 6일 배분

아래는 팀 프로젝트 당시의 개발 계획입니다. 완료 여부와 검증 결과는 상단 기록 및 연결된 증빙을 기준으로 확인합니다.
- Day 0(시작 전): 국가법령정보센터 API(OC) 신청(수동 승인 1~2일)
- Day 1: 골격 확정 + API 샘플 검증 + EC2 최초 구축(build_index) + SPEC §2·§3 freeze
- Day 2: common/rag.py 벡터DB 백엔드 다같이 구현 + 각자 인덱싱(단순 검색)
- Day 3-4: 분야별 Agent 답변 생성(Bedrock) + 인용·근거 점검 + 연락처/문서초안 연결
- Day 5: RAG 고도화(하이브리드+쿼리확장, rag.py에서 → 분야별 게이트로 적용) + 효과 비교
- Day 6: Streamlit UI(answer_blocks 카드 + 초안 렌더링) + 데모 + 발표

<a id="데이터-공공-api크롤링-0"></a>

## 법령 데이터와 구축 기록
- 근거 조문·검색 코퍼스: 국가법령정보센터에서 수집한 법령과 시행일. 아래 수치는 팀 프로젝트의 구축·실검색 검증 기록입니다.

> **구축 당시 기록** (`build_index.py all` 검증): 4분야 벡터DB 적재 완료 (로컬 Chroma · EC2 RDS pgvector) —
> labor 166 / housing 42 / consumer 192 / finance 812 조문 (합계 1,212). 전 분야 `DomainRAG.is_real=True`(stub 아님).

| 분야 | 담당 | 핵심 법령 |
|---|---|---|
| labor (노동) | A | 근로기준법, 최저임금법 |
| housing (주택) | B | 주택임대차보호법 |
| consumer (소비자) | C | 전자상거래법, 방문판매법(구독·계속거래), 할부거래법(할부·상조) |
| finance (금융·채무) | D | 채무자회생법(개인회생·파산), 통신사기피해환급법(보이스피싱), 채권추심법·대부업법 |

## 4명이 각자 RAG
- 분야마다 별도 벡터DB 컬렉션(law_labor 등). 4명이 각자 자기 컬렉션 독립 구축.
- 검색 인터페이스는 common/rag.py 하나로 통일. 하이브리드/BM25·쿼리확장은 분야별 게이트로 켜며,
  변경 전후 효과는 evaluate로 비교한다.

## 데이터 파이프라인 (medallion + 증분 갱신)
```bash
python scripts/build_index.py all    # 최초 전체 구축 (bronze→silver→gold)
python scripts/update_laws.py        # 증분 갱신 — 시행일 바뀐 법령만 재처리
```
팀 구성의 기본 갱신 주기는 **주 1회 배치**(cron 또는 Airflow DAG)입니다. 실제 제공 범위에 맞는 갱신 주기와 수집 시점 확인이 필요합니다.
manifest(data/manifest.json)가 법령별 시행일을 기억해 변경분만 갱신한다.
★팀이 채울 동적 내용은 pipeline/config.py 의 LAW_LIST 와 .env 키 뿐.★

## LangSmith 트레이싱 (Day 2부터)
.env에 `LANGSMITH_API_KEY`와 `LANGCHAIN_TRACING_V2=true`를 설정하는 추적 경로입니다.
실행 후 smith.langchain.com에서 분류→검색→검증→종합 흐름과 노드별 지연·토큰 기록 여부를 확인합니다.
verifier가 답변을 탈락시켰을 때 "왜"를 추적하는 도구이자, 발표 데모 화면.

## 3축 개선 루프 (각자 자기 분야를 측정하며 개선)
```bash
python scripts/evaluate.py labor   # 자기 분야 스코어카드 (housing/consumer/finance/all)
```
| 축 | 지표 | 의미 |
|---|---|---|
| ① 평가 | hit@k, MRR | 검색이 정답 조문을 찾는가 (evals/<분야>.jsonl 기준) |
| ② 비용 | 토큰, $/실행 | 모델 티어링(분류=Haiku/답변=Sonnet) 효과 |
| ③ 근거 점검 | grounding rate, 평균 인용 | verifier 통과 여부와 인용 수. 법률 정확성 보증과 구분 |

결과는 evals/results/<분야>_history.jsonl에 날짜별 누적 →
**baseline vs 개선안 비교표가 발표의 기술 깊이 슬라이드가 된다.**
루프: 구현 수정 → evaluate → 점수와 실패 사례 확인 → 적용 여부 결정. 필수 근거 누락은 점수와 별도로 확인합니다.

## 발표 메시지
"질문에 맞는 법령 근거와 출처를 찾아 보여주고, 등록된 공식 문의 경로와 검토할 문서 초안을 함께 제공합니다."

## 안전 (법률 도메인 주의)
- "법령 정보 안내"로 프레이밍. 단정적 자문 금지, 답변 끝 전문가 상담 권유.
- 금융은 투자 조언 아님, 법령 안내로 한정. 문서는 '초안' 명시 + 검토 권장.
- 범위 밖 질문은 정직하게 거절.

