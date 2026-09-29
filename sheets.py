import json
import asyncio
import requests

from config import (
    google_service_account, google_build,
    GOOGLE_SHEETS_ID, GOOGLE_SHEETS_TAB, GOOGLE_SHEETS_CREDENTIALS_JSON,
)


# ─── Google Sheet (log whitelist) ──────────────────────────────────────────
# Cột: No | Merchant name | MC code | IP | IP location | Status | NOTE |
#      Đề xuất thực hiện | Domain | Webhook | Webhook IP | Domain truy cập
# IP location: bot tự tra qua ip-api.com rồi ghi thẳng (xem _lookup_ip_location) —
# không có formula/trigger nào tự chạy trong sheet cho cột này.

_sheets_service = None


def _lookup_ip_location(ip: str) -> str:
    """Tra vị trí địa lý của 1 IP qua ip-api.com (free, không cần key) — format khớp
    đúng kiểu dữ liệu geolocation có sẵn trong sheet: "[IP] - City, Country - ISP".
    Best-effort: lỗi/timeout thì trả rỗng, không chặn việc ghi merchant chính."""
    try:
        resp = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,city,isp"},
            timeout=5,
        )
        data = resp.json()
        if data.get("status") != "success":
            return ""
        city = data.get("city", "")
        country = data.get("country", "")
        isp = data.get("isp", "")
        location = ", ".join(p for p in (city, country) if p)
        if not location and not isp:
            return ""
        return f"[{ip}] - {location} - {isp}"
    except Exception as e:
        print(f"[SHEET] Không tra được IP location cho {ip}: {e}", flush=True)
        return ""


def _lookup_ip_locations(ips: list) -> list:
    """Tra location cho nhiều IP, giữ đúng thứ tự — IP nào tra lỗi thì bỏ qua
    (không chèn dòng rỗng vào cột IP location)."""
    return [loc for ip in ips if (loc := _lookup_ip_location(ip))]


def _get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        creds = google_service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _sheets_service = google_build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service


def _merge_lines(existing: str, new_items: list) -> str:
    """Gộp thêm item mới vào 1 ô nhiều dòng (IP), bỏ qua item đã có sẵn."""
    current = [x.strip() for x in (existing or "").splitlines() if x.strip()]
    for item in new_items:
        if item not in current:
            current.append(item)
    return "\n".join(current)


_DOMAIN_COMBO_LABEL = "open-api & mpa"
_DOMAIN_COMBO_SET = {"openapi.g-pay.vn", "mpa.g-pay.vn"}


def _parse_domain_cell(existing: str) -> set:
    """Đọc ngược giá trị cột Domain truy cập thành tập domain — nhận diện nhãn combo
    "open-api & mpa" (dropdown của sheet) là viết tắt của cả 2 domain openapi + mpa."""
    existing = (existing or "").strip()
    if not existing:
        return set()
    if existing == _DOMAIN_COMBO_LABEL:
        return set(_DOMAIN_COMBO_SET)
    return {x.strip() for x in existing.split(",") if x.strip()}


def _format_domain_cell(domains: set) -> str:
    """Map tập domain sang đúng giá trị dropdown của sheet: đúng 2 domain openapi+mpa
    thì dùng nhãn combo "open-api & mpa", còn lại join bằng dấu phẩy (list thứ 3 —
    Ghub OpenAPI — chưa có lựa chọn dropdown tương ứng nên vẫn ghi dạng thô)."""
    if domains == _DOMAIN_COMBO_SET:
        return _DOMAIN_COMBO_LABEL
    return ", ".join(sorted(domains))


def _merge_domains(existing: str, new_items: list) -> str:
    """Gộp thêm domain mới vào cột Domain truy cập, giữ đúng nhãn combo nếu có."""
    current = _parse_domain_cell(existing)
    current.update(new_items)
    return _format_domain_cell(current)


_sheet_id_cache = {}


def _get_sheet_id(service, spreadsheet_id: str, title: str) -> int:
    """Lấy sheetId (gid) số của 1 tab theo tên — cần cho request định dạng ô. Cache lại vì không đổi."""
    key = (spreadsheet_id, title)
    if key not in _sheet_id_cache:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for sh in meta.get("sheets", []):
            if sh["properties"]["title"] == title:
                _sheet_id_cache[key] = sh["properties"]["sheetId"]
                break
        else:
            raise ValueError(f"Không tìm thấy tab '{title}' trong spreadsheet")
    return _sheet_id_cache[key]


def sheet_record_whitelist_sync(mc_code: str, merchant_name: str, ips: list, domains: list) -> str:
    """Tìm merchant theo MC code (cột C); có sẵn IP rồi thì nối thêm IP/domain vào
    đúng dòng. Nếu MC code đã được điền sẵn tay nhưng CHƯA có IP (dòng "đặt chỗ"),
    coi như merchant mới — ghi đầy đủ Merchant name/IP/Status/... vào đúng dòng đó
    (đỡ phải lách qua điền MC code vào ô Webhook như trước). Merchant hoàn toàn mới
    thì ghi vào DÒNG TRỐNG ĐẦU TIÊN tính từ trên xuống (ngay sau dòng data gần
    nhất) — không đẩy dòng cũ xuống, không nhảy xuống cuối sheet.
    Trả về mô tả ngắn để báo lại user. Chỉ gọi khi /apply đã thành công."""
    service = _get_sheets_service()
    tab = GOOGLE_SHEETS_TAB
    rng = f"'{tab}'!A:L"
    result = service.spreadsheets().values().get(spreadsheetId=GOOGLE_SHEETS_ID, range=rng).execute()
    rows = result.get("values", [])

    found_row_idx = None  # số dòng thật trên sheet (1-based)
    existing_ip = ""
    existing_iplocation = ""
    existing_domain = ""
    first_blank_row = None
    webhook_reserved_row = None  # dòng có người điền sẵn cột J (Webhook) dạng "MC_CODE\nURL" trước khi apply
    code_reserved_row = None  # dòng đã điền sẵn MC code (cột C) nhưng CHƯA có IP — coi như "đặt chỗ" cho merchant này
    for i, row in enumerate(rows[1:], start=2):  # bỏ dòng 1 (header)
        code = row[2].strip() if len(row) > 2 else ""
        ip_cell = row[3].strip() if len(row) > 3 else ""
        if code and code == mc_code:
            if ip_cell:
                found_row_idx = i
                existing_ip = row[3] if len(row) > 3 else ""
                existing_iplocation = row[4] if len(row) > 4 else ""
                existing_domain = row[11] if len(row) > 11 else ""
            elif code_reserved_row is None:
                # MC code có sẵn nhưng chưa có IP -> chưa phải merchant thật sự đã xử lý,
                # chỉ là dòng đặt chỗ -> ưu tiên ghi đầy đủ vào đây thay vì tạo dòng mới.
                code_reserved_row = i
        # Dòng chưa có MC code nhưng cột J (Webhook) đã được điền trước dạng "MC_CODE\nURL"
        # cho đúng merchant này — ưu tiên ghi vào đúng dòng đó thay vì dòng trống đầu tiên,
        # để không tách rời merchant khỏi Webhook đã chuẩn bị sẵn cho nó.
        if webhook_reserved_row is None and not code:
            webhook_val = row[9] if len(row) > 9 else ""
            webhook_label = webhook_val.split("\n", 1)[0].strip() if webhook_val else ""
            if webhook_label and webhook_label == mc_code:
                webhook_reserved_row = i
        # "Trống" = mọi cột B..L (Merchant name..Domain truy cập) đều trống — BỎ QUA cột A
        # (No), vì sheet này hay điền sẵn formula No cho nhiều dòng ở trước khi có merchant
        # thật (ghi đè công thức đó vô hại). Nếu chỉ coi row rỗng hoàn toàn là "trống" thì
        # bot sẽ nhảy qua các dòng đó, thậm chí có thể ghi đè nhầm dòng chỉ có sẵn
        # Webhook/Webhook IP (không phải merchant) — nên phải kiểm tra đúng từng cột.
        if first_blank_row is None and not any(
            len(row) > idx and row[idx].strip() for idx in range(1, 12)
        ):
            first_blank_row = i
    if first_blank_row is None:
        first_blank_row = len(rows) + 1  # không có dòng trống nào thì nối cuối cùng đã có data

    if found_row_idx:
        existing_ip_lines = [x.strip() for x in (existing_ip or "").splitlines() if x.strip()]
        new_ip = _merge_lines(existing_ip, ips)
        new_domain = _merge_domains(existing_domain, domains)
        # Chỉ tra location cho IP thật sự MỚI (chưa có trong existing_ip) — tránh gọi
        # API thừa và tránh chèn lại location của IP đã tồn tại từ trước.
        new_ips_only = [ip for ip in ips if ip not in existing_ip_lines]
        new_iplocation = _merge_lines(existing_iplocation, _lookup_ip_locations(new_ips_only))
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=GOOGLE_SHEETS_ID,
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": f"'{tab}'!D{found_row_idx}", "values": [[new_ip]]},
                    {"range": f"'{tab}'!E{found_row_idx}", "values": [[new_iplocation]]},
                    {"range": f"'{tab}'!L{found_row_idx}", "values": [[new_domain]]},
                ],
            },
        ).execute()
        return f"đã cập nhật dòng {found_row_idx} (merchant có sẵn)"

    # Ưu tiên dòng đã "đặt chỗ" theo MC code (cột C), rồi tới dòng đặt chỗ theo Webhook
    # (cột J), cuối cùng mới tới dòng trống đầu tiên — giữ merchant đi liền với chỗ
    # đã chuẩn bị trước cho nó thay vì tạo dòng mới riêng.
    target_row = code_reserved_row or webhook_reserved_row or first_blank_row

    # Merchant hoàn toàn mới -> tra location cho tất cả IP đang thêm.
    ip_locations = _lookup_ip_locations(ips)

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=GOOGLE_SHEETS_ID,
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": f"'{tab}'!B{target_row}:I{target_row}", "values": [[
                    merchant_name,          # Merchant name (tên công ty đầy đủ, gõ riêng khi /whitelist)
                    mc_code,                # MC code
                    "\n".join(ips),         # IP
                    "\n".join(ip_locations),  # IP location — bot tự tra qua ip-api.com
                    "done",                 # Status
                    "",                     # NOTE
                    "áp dụng whitelist",    # Đề xuất thực hiện
                    "",                     # Domain
                ]]},
                {"range": f"'{tab}'!L{target_row}", "values": [[
                    _format_domain_cell(set(domains)),  # Domain truy cập (map đúng nhãn combo dropdown)
                ]]},
            ],
        },
    ).execute()

    
    try:
        sheet_id = _get_sheet_id(service, GOOGLE_SHEETS_ID, tab)
        service.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SHEETS_ID,
            body={"requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": target_row - 1,
                            "endRowIndex": target_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": 12,
                        },
                        "cell": {"userEnteredFormat": {
                            "textFormat": {"strikethrough": False},
                            "wrapStrategy": "WRAP",
                            "verticalAlignment": "MIDDLE",
                        }},
                        "fields": "userEnteredFormat.textFormat.strikethrough,"
                                  "userEnteredFormat.wrapStrategy,"
                                  "userEnteredFormat.verticalAlignment",
                    }
                },
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": target_row - 1,
                            "endIndex": target_row,
                        }
                    }
                },
            ]},
        ).execute()
    except Exception as e:
        print(f"[SHEET] Bỏ qua chỉnh định dạng/chiều cao dòng (lỗi: {e})", flush=True)

    
    no_value = "1" if target_row <= 2 else f"=A{target_row - 1}+1"
    service.spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range=f"'{tab}'!A{target_row}",
        valueInputOption="USER_ENTERED",
        body={"values": [[no_value]]},
    ).execute()
    if code_reserved_row:
        return f"đã thêm dòng mới tại dòng {target_row} (khớp theo MC code đã điền sẵn)"
    if webhook_reserved_row:
        return f"đã thêm dòng mới tại dòng {target_row} (khớp theo Webhook đã điền sẵn)"
    return f"đã thêm dòng mới tại dòng {target_row}"


async def sheet_record_whitelist(mc_code: str, merchant_name: str, ips: list, domains: list) -> str:
    return await asyncio.to_thread(sheet_record_whitelist_sync, mc_code, merchant_name, ips, domains)


def translate_sheet_error(e: Exception) -> str:
    """Dịch lỗi Google Sheets API sang tiếng Việt dễ hiểu — thay vì hiện nguyên
    HttpError/JSON (dài, tiếng Anh, khó đọc) ra Telegram."""
    msg = str(e)
    low = msg.lower()
    if "protected cell" in low or "protected range" in low or "protected object" in low:
        return ("Sheet đang bị khoá (protected range) — cần thêm service account "
                "vào danh sách được sửa: Data → Protected sheets and ranges → Set permissions.")
    if "permission" in low and ("denied" in low or "does not have" in low):
        return "Bot không có quyền truy cập sheet này — kiểm tra đã share sheet cho service account chưa."
    if "unable to parse range" in low or "invalid range" in low:
        return "Sai tên tab (GOOGLE_SHEETS_TAB) — không tìm thấy tab tương ứng trong sheet."
    if "requested entity was not found" in low:
        return "Không tìm thấy Google Sheet — kiểm tra lại GOOGLE_SHEETS_ID."
    if "quota" in low or "rate limit" in low or "429" in low:
        return "Google Sheets API bị giới hạn tần suất (quota) — thử lại sau ít phút."
    if "invalid_grant" in low or "invalid credentials" in low or "jwt" in low:
        return "Service account JSON không hợp lệ hoặc hết hạn — kiểm tra lại GOOGLE_SHEETS_CREDENTIALS_JSON."
    # Không nhận diện được — rút gọn message gốc cho dễ đọc thay vì đổ nguyên traceback
    short = msg.splitlines()[0][:300]
    return f"Lỗi không xác định: {short}"
