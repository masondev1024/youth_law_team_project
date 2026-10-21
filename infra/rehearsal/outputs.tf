output "region" {
  description = "배포 리전"
  value       = var.aws_region
}

output "instance_id" {
  description = "SSM 연결 대상 인스턴스 ID"
  value       = aws_instance.app.id
}

output "instance_arn" {
  description = "종료 예약이 대상으로 삼는 인스턴스 ARN"
  value       = aws_instance.app.arn
}

output "root_volume_id" {
  description = "암호화된 루트 EBS 볼륨 ID"
  value       = aws_instance.app.root_block_device[0].volume_id
}

output "data_volume_id" {
  description = "암호화된 데이터 EBS 볼륨 ID"
  value       = one(aws_instance.app.ebs_block_device).volume_id
}

output "backup_bucket" {
  description = "비공개 버전 관리 S3 백업/배포 버킷"
  value       = aws_s3_bucket.backups.bucket
}

output "authority_table" {
  description = "삭제 권위 전용 DynamoDB 테이블"
  value       = aws_dynamodb_table.authority.name
}

output "authority_writer_function" {
  description = "합법한 삭제 의도 추가만 대행하는 제한 Lambda 이름"
  value       = aws_lambda_function.authority_writer.function_name
}

output "authority_writer_log_group" {
  description = "1일 보관 Lambda 로그 그룹"
  value       = aws_cloudwatch_log_group.authority_writer.name
}

output "ssm_start_port_forwarding_command" {
  description = "앱에 연결할 로컬 8017 포트 포워딩 명령"
  value       = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.app.id} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"8017\"],\"localPortNumber\":[\"8017\"]}'"
}

output "termination_schedule_name" {
  description = "계정 측 12시간 후 인스턴스 종료 예약 이름"
  value       = aws_scheduler_schedule.terminate.name
}

output "expires_at_utc" {
  description = "Terraform 리소스 생성 시점 기준 12시간 후 예약된 종료 시각"
  value       = local.expires_at
}
