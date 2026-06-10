# ─── Core ──────────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name used as a prefix for all resource names."
  type        = string
  default     = "attest"
}

variable "portal_bucket_name" {
  description = "Globally-unique S3 bucket name for the evidence portal."
  type        = string
}

# ─── GitHub ────────────────────────────────────────────────────────────────────

variable "github_org" {
  description = "GitHub organisation or username that owns the repository."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name (without the org prefix)."
  type        = string
  default     = "SOC_Employ_magment_Portal"
}

variable "create_oidc_provider" {
  description = "Set to false if a GitHub OIDC provider already exists in this AWS account."
  type        = bool
  default     = true
}

variable "existing_oidc_provider_arn" {
  description = "ARN of an existing GitHub OIDC provider. Used when create_oidc_provider = false."
  type        = string
  default     = ""
}

# ─── Feature flags ─────────────────────────────────────────────────────────────

variable "enable_provisioning" {
  description = "When true, attaches a scoped IAM provisioning policy to the GitHub Actions role."
  type        = bool
  default     = false
}

variable "enable_real_provisioning" {
  description = "When true, Lambda approval_handler creates real IAM users under attest-managed/*."
  type        = bool
  default     = false
}

variable "enable_worm" {
  description = "When true, enables S3 Object Lock (GOVERNANCE mode) on the portal bucket."
  type        = bool
  default     = false
}

variable "enable_ses" {
  description = "When true, creates SES resources for email-based tech lead approval."
  type        = bool
  default     = false
}

# ─── SES / Notifications ──────────────────────────────────────────────────────

variable "ses_sender_email" {
  description = "Verified SES email address used to send approval notifications."
  type        = string
  default     = ""
}

variable "tech_lead_email" {
  description = "Tech lead email address that receives approval requests."
  type        = string
  default     = ""
}

variable "portal_url" {
  description = "Base URL of the Streamlit portal (used in approval email links)."
  type        = string
  default     = "http://localhost:8501"
}

# ─── Secrets ───────────────────────────────────────────────────────────────────

variable "anthropic_api_key" {
  description = "Anthropic API key injected into the offer_processor Lambda as an env var."
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_token" {
  description = "GitHub PAT injected into the signed_processor Lambda to dispatch workflows."
  type        = string
  sensitive   = true
  default     = ""
}

# ─── IAM Policy ARNs ──────────────────────────────────────────────────────────

variable "readonly_policy_arn" {
  description = "ARN of the AWS managed policy granted to 'fresher' employees."
  type        = string
  default     = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
}

variable "developer_policy_arn" {
  description = "ARN of the AWS managed policy granted to 'experienced' employees."
  type        = string
  default     = "arn:aws:iam::aws:policy/PowerUserAccess"
}
