import re

# Bedrock cross-region inference profile for Claude Sonnet.
# All CDK stacks import from here so the model version is defined once.
BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-6-20251001-v1:0"

# Strip the cross-region prefix (e.g. "us.") to get the base foundation model
# ID used in regional ARNs, e.g.:
#   "us.anthropic.claude-sonnet-4-6-20251001-v1:0"
#   → "anthropic.claude-sonnet-4-6-20251001-v1:0"
BEDROCK_BASE_MODEL_ID = re.sub(r"^[a-z]{2}\.", "", BEDROCK_MODEL_ID)
