import os
import json

from dotenv import load_dotenv

try:
    from google.oauth2 import service_account as google_service_account
    from googleapiclient.discovery import build as google_build
except ImportError:
    google_service_account = None
    google_build = None

load_dotenv()

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
ALLOWED_GROUP_ID   = int(os.getenv("ALLOWED_GROUP_ID"))
TERRAGRUNT_DIR     = os.getenv("TERRAGRUNT_DIR")
IP_FILE            = os.getenv("IP_FILE")
GIT_BRANCH         = os.getenv("GIT_BRANCH", "master")
GITLAB_URL         = os.getenv("GITLAB_URL")
GITLAB_TOKEN       = os.getenv("GITLAB_TOKEN")
GITLAB_PROJECT_ID  = os.getenv("GITLAB_PROJECT_ID")

# Zero Trust (VPN access)
ZEROTRUST_DIR  = os.getenv("ZEROTRUST_DIR")
ZEROTRUST_FILE = os.getenv("ZEROTRUST_FILE")

# Danh sách account zero_trust để chọn khi chạy /vpn (JSON inject từ values-*.yaml).
# Mỗi phần tử: {"name": ..., "dir": <thư mục terragrunt>, "file": <terragrunt.hcl>}
ZEROTRUST_ACCOUNTS = json.loads(os.getenv("ZEROTRUST_ACCOUNTS", "[]"))
# Tương thích ngược: nếu chưa khai báo ZEROTRUST_ACCOUNTS thì dùng 1 account từ ZEROTRUST_DIR/FILE
if not ZEROTRUST_ACCOUNTS and ZEROTRUST_DIR and ZEROTRUST_FILE:
    ZEROTRUST_ACCOUNTS = [{"name": "g-pay-01", "dir": ZEROTRUST_DIR, "file": ZEROTRUST_FILE}]

# Các block không phải "team" thật, không cho chọn dù đang active
ZEROTRUST_EXCLUDED_TEAMS = {"Deprecated Email", "Default Block"}

# Đọc từ env var MERCHANT_LISTS (JSON string) inject từ values-dev.yaml
MERCHANT_LISTS = json.loads(os.getenv("LISTS", '[{"name":"merchant_ips","desc":"API chính"}]'))

# Google Sheet ghi log whitelist (tuỳ chọn — bỏ trống thì bot bỏ qua bước này)
GOOGLE_SHEETS_ID              = os.getenv("GOOGLE_SHEETS_ID", "")
GOOGLE_SHEETS_TAB             = os.getenv("GOOGLE_SHEETS_TAB", "")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
