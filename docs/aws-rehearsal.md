# AWS 개발자 리허설

합성 자료를 사용한 초기 개발자 리허설의 실행 경계와 재현 절차다. 기록된 실행은 종료됐고 AWS 자원은 정리됐다. 이후 별도 IAM 연결 재검증도 수행했으며, 그 결과는 [후속 검증 기록](engineering/iam-connection-revalidation-20261021.md)에 분리해 기록했다. 이 문서를 다시 실행하는 것은 별도 범위·비용·종료 시각 검토가 필요한 새 작업이다. 실제 참여자 검증이나 법률 품질 평가를 위한 절차가 아니다.

## 구성과 권한 경계

짧은 단일 개발자 실험에는 소형 EC2 작업자와 SQLite를 사용했다. 앱은 loopback에서만 제공하고 인바운드를 열지 않으며, 운영자는 Systems Manager로 접근한다. 일관된 SQLite 백업은 S3에 보관하고 최신 철회 권위는 DynamoDB에 둔다. 앱은 권한 상태를 읽고 좁은 Lambda 경로를 호출해 삭제 의도를 추가한다.

이 선택은 당시 측정된 워크로드와 리허설 기간에 맞춘 것이다. 다중 AZ RDS는 관리형 DB 가용성이 필요한 상시 서비스에, EKS는 여러 워크로드의 배포·확장이 필요한 경우에, MSK는 지속적인 스트림과 처리량 요구가 있을 때 검토한다. 비동기 작업이 생기면 SQS를, 알림 채널과 운영 정책이 확정되면 SNS를 검토한다. 이번 실행은 이 조건을 측정하지 않아 해당 서비스를 만들지 않았다.

Lambda는 조건부로 불변 삭제 의도를 추가하고 소유자·epoch를 비교한다. 트랜잭션에서 기본 `PutItem`·`UpdateItem` 작업을 제한하는 IAM 조건은 [DynamoDB 트랜잭션 권한 문서](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)를 따른다. 권한 상태가 없거나 오래됐거나 읽을 수 없으면 복원·제공을 보류한다.

## 운영자 소유권 설정

운영자는 자신의 로컬 AWS SSO 프로필을 사용한다. 프로필명, 계정 번호, 인스턴스 ID, 테이블명 및 데이터셋 fingerprint는 환경에 맞게 비밀이 아닌 로컬 설정에서 전달하고, 앱 인스턴스에는 운영자 자격 증명을 복사하지 않는다.

프로젝트 루트에서 초기화 명령을 사용할 때는 먼저 빈 데이터 파티션과 새 EC2 실행 상태를 확인한다. 이미 CONTROL 상태나 이력이 있으면 재초기화하지 않고 현재 상태부터 조사한다.

```sh
python -m scripts.cloud_rehearsal_operator initialize \
  --profile "$AWS_PROFILE" --table "$AUTHORITY_TABLE" \
  --fingerprint "$DATASET_FINGERPRINT" --instance "$INSTANCE_ID"
```

호스트를 교체할 때는 기존 앱과 접속을 종료하고 기존 EC2가 종료된 것을 확인한 뒤 새 호스트를 준비한다. 인계 명령은 이전 소유자 종료, 새 소유자 실행, 전체 최신 이력과 예상 owner·epoch·highwater를 확인한 경우에만 조건부 갱신한다.

```sh
python -m scripts.cloud_rehearsal_operator takeover \
  --profile "$AWS_PROFILE" --table "$AUTHORITY_TABLE" \
  --fingerprint "$DATASET_FINGERPRINT" --instance "$NEW_INSTANCE_ID"
```

정지 상태, SSM 연결 끊김, 일시적 조회 실패만으로 소유권을 넘기지 않는다. 조건 충돌이나 결과가 불명확하면 다시 초기화하지 않고 권한 상태를 조회한다. 자동 lease 만료 승계 경로는 없다.

## 백업과 복원

백업은 SQLite backup API로 일관 사본을 만들고 SHA-256, 데이터셋·정책 정보, S3 VersionId를 명세에 기록한다. 백업 성공 명세가 없거나 15분보다 오래되면 readiness가 열리지 않는다. systemd timer는 5분 간격이며, 이는 실험 설정이다. 실제 손실 한도나 RPO를 보장하지 않는다.

복원 시 서비스와 백업 timer를 멈춘다. 격리된 백업 사본의 크기·해시·버전을 검사하고, 최신 삭제 권한 상태를 적용한 뒤에만 저장소를 교체한다. 원본 백업을 수정하지 않는다. 삭제 권한을 읽을 수 없거나 소유권이 다르면 제공을 보류한다. SIGKILL 뒤 남을 수 있는 SQLite journal은 직접 삭제하지 않고 SQLite가 판단하는 복구 경로로 처리한다.

실제 소스는 [백업·복구 구현](../app/cloud_rehearsal.py), [삭제 권한 구현](../app/cloud_deletion_authority.py), [Lambda 권한 게이트](../app/cloud_authority_handler.py)에 있다. 배포 설정은 [리허설 Terraform 및 운영 문서](../infra/rehearsal/README.md)에서 확인한다.

## 재현과 검증

AMI와 Python 계열, 앱 의존성 잠금 파일을 기록한다. OS 보안 패키지 목록도 남기지만 이는 과거 OS 전체를 동일하게 공급하는 이미지 공급망 검증과 다르다. 최소 리허설 의존성 검사와 전체 개발 환경 검사 결과를 구분한다.

HTTP 흐름을 실행할 때는 readiness 응답을 기다린 후 검사 결과를 센다. 리허설 기록에는 웜, 콜드, 교체 호스트 흐름 각각 20개 검사와 29개 요청 통과가 남아 있다. 이 한 번의 실행은 대표 부하, 지연 분포, 법률 품질이나 운영 SLA를 증명하지 않는다. 결과와 한계는 [엔지니어링 결정 기록](engineering/aws-rehearsal-decisions.md) 및 [비식별 증거 JSON](evidence/aws-rehearsal-20261021.json)에 정리했다.

## 종료 확인

종료할 때 앱과 EC2를 멈추고 해당 실행의 데이터·루트 볼륨을 확인한다. S3 객체 버전, 삭제 마커, 미완료 업로드를 제거하고 복원 가능한 사본이 없는지 확인한다. 그 다음 DynamoDB 삭제 권위를 마지막으로 제거하고, 실행별 Lambda·로그·IAM 역할·예약·네트워크 인벤토리를 대조한다. 계정의 기존 자원은 범위에 넣지 않는다.

이번 실행은 Terraform destroy 31개 항목, 최종 감사 점검 37항목, 잔여 0, 오류 0을 기록했다. 31은 destroy 항목 수이고 37은 점검 수이지 고유 자원 수가 아니다. 이는 해당 리허설의 정리 결과다. AWS 비용의 청구 반영은 사용 직후 상태와 다를 수 있다.
