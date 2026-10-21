"""FastAPI 서버 — 웹서비스 진입점.

실행:  uvicorn app.api:app --host 0.0.0.0 --port 8000
화면:  GET /            → Jinja 메인 페이지 (질문 폼 + 결과 카드, JS fetch)
API:   POST /api/consult → 상담 (분류·답변·근거·연락처)
       POST /api/draft   → 문서 초안 생성
       DELETE /api/session/{session_id} → 멀티턴 세션 리셋
       GET  /health      → 헬스체크 (배포·모니터링용)
문서:  GET /docs         → Swagger UI 자동 생성

UI 확장 시: app/templates/ 에 Jinja 템플릿 추가 (base.html 상속).
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import service
from app.preparation_boundary import install_preparation_boundary
from app.preparation_routes import router as preparation_router
from app.schemas import ConsultRequest, ConsultResponse, DraftRequest, DraftResponse

_HERE = Path(__file__).resolve().parent

app = FastAPI(
    title="청년 생활법률 상담 AI",
    description="현행 법령 근거 + 공식 연락처 + 문서초안 — 노동/주택/소비자/금융",
    version="0.1.0",
)
app.state.preparation_only = False
install_preparation_boundary(app, preparation_only=False)
app.include_router(preparation_router)
app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
templates = Jinja2Templates(directory=_HERE / "templates")


@app.on_event("startup")
def _warm_embedding_model() -> None:
    """부팅 시 임베딩 모델(ko-sroberta)을 백그라운드로 미리 로드.
    최초 1회 로딩(~수초~십수초)을 첫 사용자 요청에서 떼어내, 첫 상담 지연을 없앤다.
    서버 부팅·/health 응답은 막지 않도록 데몬 스레드에서 로드한다.
    """
    import threading

    def _load() -> None:
        from common.rag import _get_model
        _get_model()

    threading.Thread(target=_load, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """메인 화면 (Jinja). 질문 입력 → JS가 /api/consult 호출 → 카드 렌더링."""
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/consult", response_model=ConsultResponse)
def consult(req: ConsultRequest):
    return service.consult(req.question, req.session_id)


@app.post("/api/draft", response_model=DraftResponse)
def draft(req: DraftRequest):
    return service.make_draft(req.question, req.domain)


@app.delete("/api/session/{session_id}")
def clear_session(session_id: str):
    return service.clear_session(session_id)


@app.get("/health")
def health():
    return {"status": "ok"}
