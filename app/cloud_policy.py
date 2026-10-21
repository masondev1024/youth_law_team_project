"""Public, synthetic-only policy bytes for the isolated AWS rehearsal release."""

from __future__ import annotations

import hashlib
import json

from app.participant_models import NOTICE_VERSION, POLICY_VERSION

RELEASE = "aws-synthetic-rehearsal-20261021-v1"
NOTICE = (
    f"개발자 전용 AWS 리허설 안내 — {NOTICE_VERSION}\n"
    "독립 참가자는 없으며 실제 참가자 모집과 접수는 닫혀 있습니다.\n"
    "개발자가 가상 사례로 동의, 상담 준비 카드, 관찰 기록과 철회를 점검합니다.\n"
    "실제 채무, 연락처, 계좌, 주민등록번호 등 개인 정보를 입력하지 마세요.\n"
    "외부 모델 호출과 녹화, 보상 지급은 하지 않습니다. 전문가 검토는 대기 중입니다.\n"
    "카드는 공식 안내를 정리한 상담 준비 자료이며 법률 판단을 제공하지 않습니다.\n"
    "기록은 늦어도 30일, 요청 로그는 24시간까지이며 이번 AWS 검증 종료 후에는\n"
    "이번에 만든 기록과 백업을 모두 삭제합니다. 철회 이력은 백업 복원에도 적용합니다.\n"
    "이 리허설은 실제 사용자 효과나 법률 정확성의 검증 결과가 아닙니다.\n"
).encode("utf-8")
POLICY = json.dumps(
    {
        "release": RELEASE,
        "versions": {"notice": NOTICE_VERSION, "policy": POLICY_VERSION},
        "scope_and_admission": {
            "origin": "developer_rehearsal",
            "real_participant_admission": False,
            "recruitment_authorized": False,
        },
        "synthetic_only": True,
        "external_models": False,
        "recording": False,
        "compensation": "none_developer_only",
        "retention": {"records_days": 30, "logs_hours": 24, "delete_after_run": True},
    },
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
DIGESTS = (hashlib.sha256(NOTICE).hexdigest(), hashlib.sha256(POLICY).hexdigest())
