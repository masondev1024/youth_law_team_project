variable "aws_region" {
  description = "리허설 전용 리전. 서울 리전을 기본값으로 사용합니다."
  type        = string
  default     = "ap-northeast-2"

  validation {
    condition     = var.aws_region == "ap-northeast-2"
    error_message = "이 리허설 구성은 ap-northeast-2만 지원합니다."
  }
}

variable "ami_id" {
  description = "사전 확인한 Canonical Ubuntu 24.04 LTS x86_64 gp3 AMI ID입니다."
  type        = string

  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.ami_id))
    error_message = "ami_id는 ami-로 시작하는 유효한 8~17자리 16진수 값이어야 합니다."
  }
}

variable "run_id" {
  description = "리허설 실행을 식별하는 짧은 영문·숫자·하이픈 값입니다."
  type        = string
  default     = "deletion-restore"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,24}$", var.run_id))
    error_message = "run_id는 소문자, 숫자, 하이픈으로 3~25자여야 합니다."
  }
}

variable "dataset_id" {
  description = "더미 데이터셋의 고정 식별자입니다. 실제 데이터 식별자를 넣지 마세요."
  type        = string
  default     = "synthetic-rehearsal-v1"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$", var.dataset_id))
    error_message = "dataset_id에는 3~64자의 영문, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다."
  }
}

variable "instance_type" {
  description = "비용이 제한된 단일 x86 리허설 인스턴스입니다."
  type        = string
  default     = "t3.small"

  validation {
    condition     = var.instance_type == "t3.small"
    error_message = "이 리허설 모듈은 비용·메모리 기준 검증을 위해 t3.small만 허용합니다."
  }
}
