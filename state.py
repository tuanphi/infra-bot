from concurrent.futures import ThreadPoolExecutor

pending = {}              # chat_id -> info để /apply, /cancel
pending_vpn = {}          # chat_id -> {"emails", "teams", "selected"}
pending_whitelist = {}    # chat_id -> {"merchant_ips", "queue", "current_idx", "selected_per_merchant", "current_selected"}
awaiting_whitelist_text = set()  # chat_id đang chờ nhập merchant/IP, không cần gõ lại /whitelist
awaiting_whitelist_merchant_name = set()  # chat_id đang chờ nhập tên công ty đầy đủ (Merchant name) cho merchant hiện tại
awaiting_whitelist_email = set()  # chat_id đang chờ nhập email người yêu cầu whitelist cho merchant hiện tại
awaiting_vpn_text = set()        # chat_id đang chờ nhập email, không cần gõ lại /vpn
executor = ThreadPoolExecutor(max_workers=2)
