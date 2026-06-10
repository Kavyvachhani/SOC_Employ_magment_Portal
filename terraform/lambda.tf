# ─── IAM: Lambda execution role ──────────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name        = "${var.project_name}-lambda-exec"
  description = "Execution role for Attest Lambda functions."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaAssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic_exec" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# S3 portal read/write + KMS for all Lambda functions
resource "aws_iam_role_policy" "lambda_portal_rw" {
  name = "portal-rw"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "VaultReadWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:HeadObject",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.portal.arn,
          "${aws_s3_bucket.portal.arn}/*",
        ]
      },
      {
        Sid    = "KMSForVault"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey",
        ]
        Resource = aws_kms_key.portal.arn
      },
    ]
  })
}

# SES — send approval + notification emails (all Lambdas)
resource "aws_iam_role_policy" "lambda_ses" {
  count = var.enable_ses ? 1 : 0
  name  = "ses-send"
  role  = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SESSend"
        Effect = "Allow"
        Action = [
          "ses:SendEmail",
          "ses:SendRawEmail",
        ]
        Resource = "*"
      },
    ]
  })
}

# IAM user provisioning — only when real provisioning is enabled
resource "aws_iam_role_policy" "lambda_iam_provisioning" {
  count = var.enable_real_provisioning ? 1 : 0
  name  = "iam-provisioning"
  role  = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CreateManagedUsers"
        Effect = "Allow"
        Action = [
          "iam:CreateUser",
          "iam:DeleteUser",
          "iam:AttachUserPolicy",
          "iam:DetachUserPolicy",
          "iam:ListAttachedUserPolicies",
          "iam:GetUser",
          "iam:TagUser",
          "iam:CreateLoginProfile",
          "iam:CreateAccessKey",
        ]
        Resource = "arn:aws:iam::${local.account_id}:user/attest-managed/*"
      },
    ]
  })
}

# ─── Placeholder Lambda zips (replaced by GitHub Actions on first deploy) ─────

data "archive_file" "offer_processor_placeholder" {
  type        = "zip"
  output_path = "${path.module}/.build/offer_processor_placeholder.zip"
  source {
    content  = "def handler(event, context):\n    print('placeholder — deploy via GitHub Actions')\n"
    filename = "offer_processor.py"
  }
}

data "archive_file" "signed_processor_placeholder" {
  type        = "zip"
  output_path = "${path.module}/.build/signed_processor_placeholder.zip"
  source {
    content  = "def handler(event, context):\n    print('placeholder — deploy via GitHub Actions')\n"
    filename = "signed_processor.py"
  }
}

data "archive_file" "approval_handler_placeholder" {
  type        = "zip"
  output_path = "${path.module}/.build/approval_handler_placeholder.zip"
  source {
    content  = "def handler(event, context):\n    return {'statusCode': 200, 'body': 'placeholder'}\n"
    filename = "approval_handler.py"
  }
}

# ─── Lambda functions ─────────────────────────────────────────────────────────

resource "aws_lambda_function" "offer_processor" {
  function_name    = "${var.project_name}-offer-processor"
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "offer_processor.handler"
  filename         = data.archive_file.offer_processor_placeholder.output_path
  source_code_hash = data.archive_file.offer_processor_placeholder.output_base64sha256
  timeout          = 60
  memory_size      = 512

  environment {
    variables = {
      ANTHROPIC_API_KEY = var.anthropic_api_key
      S3_BUCKET         = var.portal_bucket_name
    }
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_lambda_function" "signed_processor" {
  function_name    = "${var.project_name}-signed-processor"
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "signed_processor.handler"
  filename         = data.archive_file.signed_processor_placeholder.output_path
  source_code_hash = data.archive_file.signed_processor_placeholder.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      GITHUB_TOKEN     = var.github_token
      GITHUB_ORG       = var.github_org
      GITHUB_REPO      = var.github_repo
      S3_BUCKET        = var.portal_bucket_name
      ENABLE_SES       = var.enable_ses ? "true" : "false"
      SES_SENDER_EMAIL = var.ses_sender_email
      TECH_LEAD_EMAIL  = var.tech_lead_email
      APPROVAL_API_URL = aws_apigatewayv2_api.approval.api_endpoint
      # Named to match GitHub secret names used by portal
      PROJECT_GITHUB_TOKEN = var.github_token
      PROJECT_GITHUB_ORG   = var.github_org
    }
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_lambda_function" "approval_handler" {
  function_name    = "${var.project_name}-approval-handler"
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "approval_handler.handler"
  filename         = data.archive_file.approval_handler_placeholder.output_path
  source_code_hash = data.archive_file.approval_handler_placeholder.output_base64sha256
  timeout          = 60
  memory_size      = 512

  environment {
    variables = {
      S3_BUCKET                = var.portal_bucket_name
      ENABLE_REAL_PROVISIONING = var.enable_real_provisioning ? "true" : "false"
      ENABLE_SES               = var.enable_ses ? "true" : "false"
      SES_SENDER_EMAIL         = var.ses_sender_email
      TECH_LEAD_EMAIL          = var.tech_lead_email
      READONLY_POLICY_ARN      = var.readonly_policy_arn
      DEVELOPER_POLICY_ARN     = var.developer_policy_arn
      PORTAL_URL               = var.portal_url
      # Named to match GitHub secret names used by portal
      PROJECT_GITHUB_TOKEN = var.github_token
      PROJECT_GITHUB_ORG   = var.github_org
      GITHUB_REPO          = var.github_repo
    }
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

# ─── Lambda permissions ───────────────────────────────────────────────────────

# S3 → offer_processor
resource "aws_lambda_permission" "s3_invoke_offer_processor" {
  statement_id   = "AllowS3InvokeOfferProcessor"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.offer_processor.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.portal.arn
  source_account = local.account_id
}

# S3 → signed_processor
resource "aws_lambda_permission" "s3_invoke_signed_processor" {
  statement_id   = "AllowS3InvokeSignedProcessor"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.signed_processor.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.portal.arn
  source_account = local.account_id
}

# API Gateway → approval_handler
resource "aws_lambda_permission" "apigw_invoke_approval" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.approval_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.approval.execution_arn}/*/*"
}

# ─── S3 bucket notifications — SUFFIX-FILTERED to prevent loops ──────────────
#
# offer-letter.pdf  → offer_processor  (only this file triggers it)
# signed-nda.pdf    → signed_processor (only this file triggers it)
#
# offer_processor writes employee.json + nda-unsigned.pdf — neither suffix
# matches any trigger, so there is no feedback loop.

resource "aws_s3_bucket_notification" "portal_triggers" {
  bucket = aws_s3_bucket.portal.id
  depends_on = [
    aws_lambda_permission.s3_invoke_offer_processor,
    aws_lambda_permission.s3_invoke_signed_processor,
  ]

  lambda_function {
    lambda_function_arn = aws_lambda_function.offer_processor.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "employees/"
    filter_suffix       = "offer-letter.pdf"
  }

  lambda_function {
    lambda_function_arn = aws_lambda_function.signed_processor.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "employees/"
    filter_suffix       = "signed-nda.pdf"
  }
}

# ─── CloudWatch log groups (pre-create to set retention) ─────────────────────

resource "aws_cloudwatch_log_group" "offer_processor" {
  name              = "/aws/lambda/${aws_lambda_function.offer_processor.function_name}"
  retention_in_days = 90
}

resource "aws_cloudwatch_log_group" "signed_processor" {
  name              = "/aws/lambda/${aws_lambda_function.signed_processor.function_name}"
  retention_in_days = 90
}

resource "aws_cloudwatch_log_group" "approval_handler" {
  name              = "/aws/lambda/${aws_lambda_function.approval_handler.function_name}"
  retention_in_days = 90
}
