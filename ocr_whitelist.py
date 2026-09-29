import io
import re
import unicodedata

import pytesseract
from PIL import Image

from common import extract_domain
from config import MERCHANT_LISTS


# ─── OCR ảnh form đề nghị whitelist (chạy local trong container — không gửi ảnh
# ra ngoài) ──────────────────────────────────────────────────────────────────

def _strip_diacritics(s: str) -> str:
    """Bỏ hết dấu tiếng Việt (kể cả đ/Đ, NFD không tự tách) rồi lowercase — OCR
    hay đọc thiếu/sai dấu (vd 'Họ' -> 'Ho', 'nghị' -> 'nghi'), nên so khớp label
    ở dạng không dấu để không phụ thuộc OCR đoán đúng từng dấu."""
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()

# Thứ tự các label PHẢI khớp đúng thứ tự xuất hiện trong ảnh form (Google Form
# response) — dùng để cắt text OCR thành từng khối "label -> giá trị nằm giữa
# label này và label kế tiếp". Pattern viết ở dạng không dấu, so với text đã
# _strip_diacritics().
_LABELS = [
    ("requester_name", r"ho va ten nguoi.*nghi"),
    ("requester_email", r"email nguoi.*nghi"),
    ("department", r"phong ban"),
    ("merchant_name", r"ten merchant"),
    ("merchant_code", r"merchant ?code"),
    ("ip_raw", r"ip cua merchant"),
    ("domain_raw", r"domain api truy cap"),
    # Không phải field cần đọc — chỉ để CHẶN domain_raw nuốt hết phần "Approval
    # Workflow" nằm phía dưới trong ảnh (domain_raw là label cuối cùng cần đọc,
    # nếu không có điểm dừng thì nó ăn luôn mọi dòng còn lại tới hết OCR).
    ("_stop", r"approval workflow"),
]

def _normalize_domain_chars(s: str) -> str:
    """Bỏ hết dấu chấm/phẩy/gạch ngang/khoảng trắng, chỉ giữ chữ+số rồi lowercase —
    để so khớp domain không phụ thuộc OCR đọc đúng từng dấu chấm nhỏ xíu (lỗi rất
    hay gặp, vd "openapi.g-pay.vn" bị đọc thành "openapi,g-payvn" — mất/nhầm cả 2
    dấu chấm, nếu bắt buộc phải có dấu chấm mới nhận ra host thì khớp trượt)."""
    return re.sub(r'[^a-zA-Z0-9]', '', s).lower()


def extract_text(image_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(img, lang="vie+eng")


def parse_whitelist_form(text: str) -> dict:
    """Bóc tách form đề nghị whitelist từ text OCR. Layout Google Form response
    luôn là: <label>\\n<giá trị>\\n<label kế tiếp>\\n... nên chỉ cần tìm vị trí
    từng label rồi lấy các dòng nằm giữa 2 label liên tiếp làm giá trị — không
    cần hiểu bố cục ảnh, chỉ cần label xuất hiện đúng thứ tự."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    stripped_lines = [_strip_diacritics(l) for l in lines]

    positions = []  # [(key, line_idx)]
    for i, line in enumerate(stripped_lines):
        for key, pattern in _LABELS:
            if re.search(pattern, line):
                positions.append((key, i))
                break

    values = {key: "" for key, _ in _LABELS}
    for idx, (key, line_idx) in enumerate(positions):
        end_idx = positions[idx + 1][1] if idx + 1 < len(positions) else len(lines)
        value_lines = [v.strip(" :*•+-") for v in lines[line_idx + 1:end_idx]]
        values[key] = "\n".join(v for v in value_lines if v)

    ips = re.findall(r'(?:\d{1,3}\.){3}\d{1,3}', values["ip_raw"])

    return {
        "requester_name": values["requester_name"].strip(),
        "requester_email": values["requester_email"].strip(),
        "merchant_name": values["merchant_name"].strip(),
        "merchant_code": values["merchant_code"].strip(),
        "ips": ips,
        "domain_raw": values["domain_raw"].strip(),
    }


def match_lists_by_domain(domain_raw: str) -> list:
    """Map domain đọc được từ ảnh -> đúng list trong MERCHANT_LISTS. So khớp fuzzy
    (_normalize_domain_chars ở cả 2 phía) thay vì đòi domain_raw phải chứa đúng
    format host có dấu chấm — chịu được lỗi OCR đọc nhầm/mất dấu chấm."""
    normalized_raw = _normalize_domain_chars(domain_raw)
    matched = []
    for lst in MERCHANT_LISTS:
        domain = extract_domain(lst.get("desc", ""))
        if domain and _normalize_domain_chars(domain) in normalized_raw:
            matched.append(lst)
    return matched
