aws_region           = "us-east-1"
# Must match the deployed bucket in tfstate; changing it forces destroy/recreate.
portal_bucket_name   = "attest-vault-669167971016"
github_org           = "Kavyvachhani"
github_repo          = "SOC_Employ_magment_Portal"

create_oidc_provider       = false
existing_oidc_provider_arn = "arn:aws:iam::669167971016:oidc-provider/token.actions.githubusercontent.com"

enable_provisioning       = true
enable_real_provisioning  = true
enable_worm               = false
enable_ses                = false
# Must stay "attest" — all live resources (Lambdas, roles, KMS, bucket) carry
# this prefix; changing it makes terraform destroy/recreate everything.
project_name = "attest"
