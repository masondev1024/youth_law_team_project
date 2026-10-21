# AWS 법률 AI 삭제·복구 리허설 구성

이 문서는 Terraform·systemd 소스가 정의하는 실행 구성을 설명합니다. 2026-10-21 합성 데이터 리허설은 이미 완료했고 해당 AWS 자원도 정리했습니다. 현재 소스를 실제 배포 상태로 오해하지 않도록, AWS에서 관찰한 결과는 [비식별 실행 증거](../../docs/evidence/aws-rehearsal-20261021.json)에 별도로 기록했습니다. 원래 실행 뒤 소스가 바뀐 범위와 해시 한계는 [엔지니어링 기록](../../docs/engineering/aws-rehearsal-decisions.md)에 있습니다.

리허설은 개발자용이며 전체 RAG·Bedrock 배포, 전문가 검토 또는 실제 참가자 검증을 뜻하지 않습니다. 새 실행에는 별도 범위·비용·종료 시각 확인이 필요합니다. 참가자 데이터나 로컬 DB를 업로드하지 않습니다.

## 구성 경계

- 서울(`ap-northeast-2`)의 새 VPC, 퍼블릭 서브넷, 인터넷 게이트웨이와 기본 경로
- 인바운드 규칙이 없는 SG. 인스턴스 egress는 HTTPS와 VPC DNS로 제한
- 입력으로 고정한 Canonical Ubuntu 24.04 x86_64 gp3 AMI, `t3.small`, CPU credit `standard`, IMDSv2 필수
- 암호화 gp3 루트·데이터 볼륨 각 10 GiB. 두 볼륨은 EC2 종료 때 삭제
- SSM 관리 전용 역할과 앱 데이터 역할 분리
- 비공개·버전 관리·SSE-S3 버킷, 하루 후 현재/이전 버전 및 미완료 업로드 정리
- 온디맨드 과금 DynamoDB 권위 테이블(`pk`/`sk`), PITR·TTL 없음
- 앱과 권한 게이트 Lambda의 1일 보관 CloudWatch 로그 그룹
- 단일 파일 Python 3.12 Lambda 삭제 이력 추가 게이트(256 MiB, 10초 timeout). 예약 동시성을 두지 않음(`reserved_concurrent_executions = -1`, 미예약 상태)
- 인스턴스 ID 하나만 종료하는 계정 측 12시간 1회 EventBridge Scheduler 예약

계정의 동시 실행 한도가 낮은 상황에서 예약 동시성을 잡으면 다른 함수에 남는 미예약 용량을 더 줄일 수 있어 이 리허설은 Lambda 용량을 예약하지 않습니다. 단일 소유자 앱과 조건부 쓰기는 동작 경계를 제한하지만 처리량이나 대기 시간 보장은 제공하지 않습니다.

EC2 앱 역할은 삭제 권위 표 읽기 권한만 갖습니다. 권한 있는 삭제 이력 추가는 Lambda 경로로 제한합니다. Lambda 역할은 해당 테이블의 읽기·조건부 추가 권한과 자체 로그 쓰기만 갖습니다. 두 역할 모두 테이블/항목 삭제, S3 삭제, IAM·EC2·모델 권한은 없습니다. S3 앱 권한은 `deployment/*` 읽기, `backups/*` 버전/일반 읽기와 백업 쓰기입니다. IAM만으로 개별 항목의 append-only 규칙을 강제할 수 없으므로 Lambda의 검증·조건부 트랜잭션과 운영자 초기화 절차가 필요합니다. 앱/Lambda 역할이 탈취되거나 AWS 관리자 권한으로 제어 항목이 조작되는 상황은 보장 범위 밖입니다. 전용 S3 버킷은 DynamoDB authority에 의존하도록 연결해 Terraform destroy에서 먼저 제거되며, 실행 manifest에서 백업 버전 0건을 확인한 뒤에만 authority를 삭제해야 합니다.

DynamoDB `TransactWriteItems` 요청의 IAM 검사는 transaction 안의 `Put`/`Update` 항목도 `dynamodb:PutItem`/`dynamodb:UpdateItem` 권한으로 평가합니다. 그래서 Lambda 정책은 이 두 권한에 `dynamodb:EnclosingOperation = TransactWriteItems` 조건을 붙여 일반 단건 덮어쓰기를 막습니다. [AWS DynamoDB transaction IAM 문서](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)

## 사전 조건과 로컬 검증

새 리허설을 별도로 승인받아 재현할 경우 Terraform 1.6 이상, AWS CLI, 운영자 자격증명, 사전 읽기 확인으로 고른 Canonical Ubuntu 24.04 LTS x86_64 gp3 AMI ID가 필요합니다. AMI ID는 `ami_id` 입력으로 고정하며 최신 이미지 자동 선택을 하지 않습니다. 아래 단계는 로컬 포맷·초기화·정적 검증만 수행하며 `plan`/`apply` 및 AWS 리소스 생성을 포함하지 않습니다. 기본 backend는 로컬이므로 이 디렉터리의 state를 비밀처럼 취급하고 절대 커밋하지 마세요.

```bash
cd infra/rehearsal
terraform fmt -check
terraform init -backend=false
terraform validate
```

`terraform init`은 provider를 다운로드할 수 있습니다. `.terraform/`와 상태·계획 파일은 로컬 전용입니다. provider lock 파일은 재현을 위해 소스와 함께 관리합니다. AWS API 읽기 확인 및 plan 검토는 별도 승인된 실행 시점에 진행합니다.

## 인스턴스 초기화와 앱 구동 순서

cloud-init은 Python venv/ACL/전송 부트스트랩 도구와 SSM Agent만 준비하고, UID 1001인 비특권 `youthlaw` 사용자와 데이터 디스크의 `/var/lib/youthlaw` 마운트를 설정합니다. 마운트 루트는 해당 UID 소유 `0700`이고, 앱 저장소 경로는 `app.cloud_rehearsal init`이 안전하게 생성합니다. UID를 고정해 복구 볼륨을 교체해도 소유권 해석이 유지됩니다. 앱 코드, 토큰, `cloud.json`, 정책 번들, 로컬 DB는 설치하거나 업로드하지 않으며 앱 서비스를 켜지 않습니다. Ubuntu 패키지 저장소 URL은 첫 패키지 요청 전에 HTTPS로 전환합니다.

운영자는 로컬에서 승인된 고정 의존성 번들과 정책을 검증하고, 버킷의 `deployment/` 접두사에 배치한 다음 인스턴스에 설치해야 합니다. `/etc/youthlaw/cloud.json`은 UID 1001 소유 `0600`으로 설정해 앱만 읽게 합니다. 토큰을 Terraform, user-data, 출력, Git 또는 로그에 두지 마세요. 번들/정책 검증과 설정 설치를 마친 뒤에만 아래 유닛을 인스턴스에 설치하고 활성화합니다.

```bash
sudo install -o root -g root -m 0644 youth-law-rehearsal.service /etc/systemd/system/
sudo install -o root -g root -m 0644 youth-law-rehearsal-backup.service /etc/systemd/system/
sudo install -o root -g root -m 0644 youth-law-rehearsal-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now youth-law-rehearsal.service youth-law-rehearsal-backup.timer
```

앱 명령은 `python -m app.cloud_rehearsal serve|backup --config /etc/youthlaw/cloud.json` 계약을 따릅니다. 두 systemd 서비스는 각각 512 MiB memory limit, 파일 권한 마스크 `0077`, 데이터 경로 쓰기 제한을 사용합니다. EC2의 SSM 역할은 별도의 전용 앱 역할만 맡을 수 있고, 앱 역할은 버킷 읽기·백업 쓰기·DynamoDB 조회·Lambda 호출·로그 권한만 가집니다. 백업 timer는 부팅 2분 뒤 시작하며, 이후 5분 간격(`OnUnitActiveSec=5min`, `AccuracySec=10s`)으로 SQLite 일관 사본을 요청합니다. 앱 readiness는 성공한 백업이 15분보다 오래되면 열리지 않도록 정의되어 있습니다. 5분 주기와 15분 freshness 한계는 소스 설정이며, 실제 AWS 관측이나 RPO 보장이 아닙니다. AMI ID와 앱 의존성 잠금을 고정하지만 OS apt 패키지는 최초 부팅 시 저장소에서 설치되므로 버전에 따라 달라질 수 있습니다. cloud-init은 설치된 패키지와 Python 버전을 기록하지만 OS 전체의 재현 가능한 잠금은 아닙니다.

## 삭제 권위 시작과 인스턴스 교체

Lambda에는 `append`만 있고 제어 항목 초기화·교체 기능은 없습니다. 앱 역할도 DDB 쓰기 권한이 없습니다. 별도 운영자 권한으로 `CONTROL` 항목을 먼저 설정하고 검증하기 전에는 앱 유닛을 켜지 않습니다. 저장소 루트에서 `python -m scripts.cloud_rehearsal_operator initialize|takeover`를 사용합니다. 정확한 인수와 실패 시 처리는 [운영 절차](../../docs/aws-rehearsal.md)에 있습니다. 아래는 도구가 수행하는 조건을 설명하며 수동 덮어쓰기를 권장하지 않습니다.

초기화 시 앱의 `init` 결과에서 실제 `dataset_fingerprint`를 확인하고 IMDSv2 `identity` 명령에서 현재 EC2 ID를 확인합니다. `pk`는 `DATASET#<fingerprint>`, `sk`는 `CONTROL`, `kind`는 `control`, `owner_instance_id`는 현재 인스턴스, `epoch`는 `1`, `highwater`는 `0`인 항목을 구성합니다. AWS 운영자 역할로 `aws dynamodb put-item`에 `--condition-expression 'attribute_not_exists(pk)'`를 지정해 생성합니다. 이후 `aws dynamodb get-item --consistent-read`로 다시 읽어 모든 키·식별자·초기 순번을 확인합니다. 항목이 이미 있거나 읽은 값이 다르면 덮어쓰지 말고 중단합니다.

인스턴스 교체 때는 먼저 이전 ID를 `aws ec2 describe-instances`로 조회해 상태가 `terminated`인지 확인합니다. 중지·응답 없음·SSM 연결 해제만으로 소유권을 넘기지 않습니다. 복원 데이터의 지문이 CONTROL의 지문과 같은지 확인하고, DynamoDB를 강한 일관성으로 읽어 현재 owner, epoch, highwater를 기록합니다. 운영자 역할의 `aws dynamodb update-item`은 조건에 `owner_instance_id=:old_owner AND epoch=:old_epoch AND highwater=:observed_highwater AND dataset_fingerprint=:fingerprint AND kind=:control`을 모두 넣고, 갱신은 `owner_instance_id=:new_instance`와 `epoch=:old_epoch+1`만 수행합니다. 조건 불일치나 응답 불확실이면 재시도·강제 덮어쓰기 대신 중단하고 강한 일관성 읽기로 상태부터 다시 확인합니다. 갱신 뒤 같은 읽기로 새 owner와 epoch를 확인하고, 그 이후에만 복원·서비스 기동을 계속합니다.

이 절차는 단일 실행 권위 이전을 수동으로 보호합니다. 기존 EC2가 종료됐음을 확인하지 못하면 교체하지 않으며, 이 IaC 모듈만으로 운영자 CONTROL 초기화나 교체가 실행되는 것은 아닙니다.

SSM 포트 포워딩 명령은 `terraform output -raw ssm_start_port_forwarding_command`로 확인합니다. 앱은 loopback의 8017에서만 수신하고, 이 리소스에는 인바운드 네트워크 경로가 없습니다. SSM 권한과 앱 세션 인증은 서로 다른 경계입니다.

## 종료와 잔존 정리

12시간 Scheduler는 EC2 종료를 요청하는 보조 장치입니다. 과거 실행에서는 예약 종료를 확인하고, 백업을 비운 뒤 권한 테이블을 마지막에 제거했습니다. Scheduler 자체가 모든 자원 삭제를 보장하지 않습니다. 제안 예산 $5는 강제 청구 상한이 아니므로, 새 실행은 부팅 전 비용 가정과 실제 리소스 수를 확인하고 제한 시간 안에 종료·삭제를 완료해야 합니다. 디스크 자동 삭제 후 모든 객체 버전·삭제 마커까지 확인하고 S3 버킷을 비운 다음, 복원 사본이 없는 것을 확인한 뒤 DynamoDB 삭제 권위를 마지막으로 제거합니다. 실행별 잔존 상태를 대조하세요. Terraform destroy는 생성된 전용 버킷의 모든 버전을 제거하도록 설정되어 있으므로 해당 실행의 종료와 데이터 삭제가 확인된 뒤에만 수행합니다.

```bash
terraform destroy
```

과거 실행의 최종 감사는 점검 37항목, 오류 0, 잔여 0을 기록했습니다. 이 숫자는 현재 AWS 상태가 아니라 완료된 실행의 결과이며, 37은 고유 자원 수가 아닌 감사 점검 수입니다. 새 실행 뒤에는 EC2, EBS, Scheduler, IAM, 로그 그룹, DynamoDB, S3 버전과 네트워크 구성요소가 남지 않았는지 서비스별로 조회합니다. 1일 lifecycle은 보조 정리이며 즉시 삭제 검증을 대신하지 않습니다. AWS Budgets 알림은 강제 지출 상한이 아니며 청구 반영에는 지연이 있을 수 있습니다.
