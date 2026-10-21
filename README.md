# 청년 생활법률 상담 AI

청년이 평어로 법률 고민을 입력하면(분야 선택 X) → 시스템이 분야 자동 판별 →
노동/주택/소비자/금융 전문가가 각자 RAG로 현행 법령을 찾아 답 + 공식 연락처 + 문서 초안까지 제공.

## 개인 후속 개발: 금융 첫 상담 준비

금융 문제를 처음 겪는 사람이 공식 문의 경로와 상담 전에 확인할 내용을 정리하도록 준비 카드를 추가했습니다. 팀 프로젝트에서 금융 분야 검색·Agent와 질문 분류 흐름을 맡은 뒤, 개인적으로 검색 근거의 누락과 보류 기준, 출처 확인, 인증·요청 제한·복구를 보강하고 있습니다.

특히 삭제 이전 백업을 복원했을 때 동의·관찰 기록이 다시 나타나는 문제를 다룹니다. 삭제 이력을 복원 대상 DB 밖에 보관하고, 최신 이력을 적용한 사본만 서비스에 연결하도록 구성합니다. AI 기능을 만드는 것에 더해, 사용할 근거와 실패했을 때의 동작·기록 처리까지 함께 확인하려는 후속 작업입니다.

[문제·설계·재현 방법](docs/finance-consultation-recovery.md) · [개발자 리허설 앱](app/rehearsal_app.py) · [삭제 이력과 복구](app/participant_recovery.py)

현재는 로컬 더미 검증 범위입니다. 전문가 검토는 대기 중이고 실제 참가자 접수는 닫혀 있습니다. 팀 작업·개인 후속 개발, 모델 평가·사용자 효과·운영 검사를 구분합니다.

## 차별점 (범용 AI 대비 — 구조적으로 못 하는 것)
1. **신뢰성**: 현행 법령(국가법령정보센터) 근거 + 답변에 조문 원문·출처·시행일 표시 + 환각 방지
2. **행동 연결**: 분야별 검증된 공식 연락처·링크 (LLM 생성 금지, contacts.py)
3. **문서 초안 생성**: 내용증명·진정서 초안을 법령 근거 담아 생성 (drafter.py, 빈칸은 사용자가 채움)

## 4분야 자동 라우팅
입력창 하나. 사용자는 분야를 안 고름 → Supervisor가 자동 분류.
- 1개 분야 → 전문가 1명 / 복수 분야 → 동시 실행(fan-out) → Planner 종합
- 범위 밖(형사 등) → 정직하게 거절(out_of_scope)

## 구조
```
docs/TEAM_GUIDE.md         # 협업 시작 절차 (필독)
docs/API_SETUP.md          # API 신청 절차
.github/workflows/ci.yml  # 계약 테스트 CI
state.py              # 공유 State 스키마 (SPEC §2). 공용 ⚠️
graph.py              # 그래프 조립 (fan-out/fan-in). 공용 ⚠️
common/rag.py         # 공통 RAG 헬퍼 (검색 + 고도화 지점). 공용 ⚠️
common/contacts.py    # 검증된 공식 연락처 (환각 0). 공용 ⚠️
common/drafter.py     # 문서 초안 생성. 공용 ⚠️
agents/supervisor.py  # 분야 자동 분류·라우팅. 공용 ⚠️
agents/verifier.py    # 답변-근거 검증 — 환각 도달 경로 차단(하네스). 공용 ⚠️
agents/planner.py     # 종합 + 출처 + 연락처 (검증 통과분만). 공용 ⚠️
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
- **docs/HARNESS.md** — 하네스 엔지니어링 적용 내역 (verifier·contacts·llm·CI가 환각/실수를 구조로 차단하는 방식)
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
python -m pytest tests/                  # 계약 검사 (현재 50 tests)
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
- Day 0(시작 전): 국가법령정보센터 API(OC) 신청(수동 승인 1~2일)
- Day 1: 골격 확정 + API 샘플 검증 + EC2 최초 구축(build_index) + SPEC §2·§3 freeze
- Day 2: common/rag.py 벡터DB 백엔드 다같이 구현 + 각자 인덱싱(단순 검색)
- Day 3-4: 각자 전문가 답변 생성(Bedrock) + 환각 방지 + 연락처/문서초안 연결
- Day 5: RAG 고도화(하이브리드+쿼리확장, rag.py에서 → 분야별 게이트로 적용) + 효과 비교
- Day 6: Streamlit UI(answer_blocks 카드 + 초안 렌더링) + 데모 + 발표

## 데이터 (공공 API·크롤링 0)
- 근거 조문·검색 코퍼스: 국가법령정보센터 (현행 법령 + 시행일) — **구축·실검색 검증 완료**

> **구축 현황** (`build_index.py all` 검증): 4분야 벡터DB 적재 완료 (로컬 Chroma · EC2 RDS pgvector) —
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
법령은 거의 안 바뀌므로 **주 1회 배치**(cron 또는 Airflow DAG)면 충분.
manifest(data/manifest.json)가 법령별 시행일을 기억해 변경분만 갱신한다.
★팀이 채울 동적 내용은 pipeline/config.py 의 LAW_LIST 와 .env 키 뿐.★

## LangSmith 트레이싱 (Day 2부터)
.env에 `LANGSMITH_API_KEY` 입력 + `LANGCHAIN_TRACING_V2=true`면 끝 — 코드 수정 0줄.
smith.langchain.com에서 분류→검색→검증→종합 전 흐름과 노드별 지연·토큰이 보인다.
verifier가 답변을 탈락시켰을 때 "왜"를 추적하는 도구이자, 발표 데모 화면.

## 3축 개선 루프 (각자 자기 분야를 측정하며 개선)
```bash
python scripts/evaluate.py labor   # 자기 분야 스코어카드 (housing/consumer/finance/all)
```
| 축 | 지표 | 의미 |
|---|---|---|
| ① 평가 | hit@k, MRR | 검색이 정답 조문을 찾는가 (evals/<분야>.jsonl 기준) |
| ② 비용 | 토큰, $/실행 | 모델 티어링(분류=Haiku/답변=Sonnet) 효과 |
| ③ 환각 | grounding rate, 평균 인용 | 답변이 verifier 검증을 통과하는가 |

결과는 evals/results/<분야>_history.jsonl에 날짜별 누적 →
**baseline vs 개선안 비교표가 발표의 기술 깊이 슬라이드가 된다.**
루프: 구현 수정 → evaluate → 숫자 확인 → 다시 수정. 숫자가 안 오르면 개선이 아니다.

## 발표 메시지
"범용 AI는 작년 법으로 일반론을 답한다. 우리는 현행 법령을 근거로 출처와 함께 답하고,
어디로 연락해야 하는지, 어떤 문서를 보내야 하는지까지 정확히 안내한다."

## 안전 (법률 도메인 주의)
- "법령 정보 안내"로 프레이밍. 단정적 자문 금지, 답변 끝 전문가 상담 권유.
- 금융은 투자 조언 아님, 법령 안내로 한정. 문서는 '초안' 명시 + 검토 권장.
- 범위 밖 질문은 정직하게 거절.
