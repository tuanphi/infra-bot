# Ansible Telegram bot

Bot này chạy đúng lệnh dưới đây từ thư mục gốc của source
`gitlab.g-pay.vn/devops/ansible/infra`, sau đó tạo GitLab Merge Request khi
Ansible thành công:

```bash
ansible-playbook -i nonprod gitlab-repos.yaml --tags=project_user_access
```

Chỉ có `/run <source-branch>`, `/start` và `/help`. Không chứa OCR, VPN,
Terragrunt hay Google Sheets. Source branch **phải được commit/push từ trước**
và có diff so với `GIT_TARGET_BRANCH`. Chạy một playbook hiện có không tự tạo
diff cho MR; vì chưa biết định dạng biến `project_user_access` trong repo Ansible,
bot không tự sửa playbook/inventory. GitLab repo không truy cập được trong môi
trường thiết kế này, do đó cần kiểm tra inventory `nonprod`, vị trí playbook
và các dependency role/collection tại môi trường của bạn.

## Luồng

1. Người vận hành cập nhật cấu hình Ansible trên source branch và push.
2. Thành viên có ID trong `ALLOWED_USER_IDS`, ở đúng `ALLOWED_GROUP_ID`, gửi
   `/run feature/add-user`.
3. Bot clone repo, fetch branch, kiểm tra diff, checkout **commit cố định**,
   kiểm tra file, chạy lệnh Ansible trên.
4. Ansible exit 0 và branch vẫn trỏ cùng commit: bot dùng GitLab API tìm MR
   đang mở hoặc tạo MR mới vào `GIT_TARGET_BRANCH`. MR ghi commit đã chạy.
5. Nếu clone, Ansible, hoặc kiểm tra commit lỗi thì **không tạo MR**. Việc Ansible
   đã thay đổi máy đích không thể tự rollback nếu bước GitLab API sau đó lỗi.

Việc chạy playbook có hiệu lực trên máy nonprod **trước** khi MR được merge, đúng
thứ tự yêu cầu. Chạy lại cùng branch có thể chạy lại Ansible; playbook cần có
tính idempotent. Bot chỉ cho phép một lượt chạy tại một thời điểm trong một
process; triển khai đúng **một replica** khi dùng Telegram polling.

## Runtime

`Dockerfile` dùng Python `3.8.10`, cài `ansible-core==2.12.10`,
`Jinja2==2.10.1`, `MarkupSafe==2.0.1` và xác nhận version khi build.
MarkupSafe được pin vì Jinja cũ cần `soft_unicode`. Ảnh base cũ cần được
mirror/nâng cấp theo chính sách bảo mật riêng nếu đem dùng lâu dài.

```bash
docker build -t infra-ansible-bot:1.0 .
docker run --rm --env-file .env \
  -v /secure/path/git_id_ed25519:/run/secrets/git_id_ed25519:ro \
  -v /secure/path/ansible_id_ed25519:/run/secrets/ansible_id_ed25519:ro \
  -v /secure/path/known_hosts:/root/.ssh/known_hosts:ro \
  infra-ansible-bot:1.0
```

Tạo `.env` từ `.env.example`, điền token và ID thật; **không commit `.env` hoặc
private key**. `known_hosts` phải chứa host key đã xác minh của GitLab server. Nếu GitLab dùng CA nội bộ, đưa CA vào trust store của container.
GitLab token cần quyền API tạo MR, Git SSH key cần đọc repo; Ansible SSH key
cần kết nối đúng host nonprod. Nếu playbook dùng Ansible Vault, bổ sung secret
và `ANSIBLE_VAULT_PASSWORD_FILE` vào runtime. Nếu repo khai báo roles/collections,
cài đúng phiên bản theo repo trước khi chạy.

`GITLAB_PROJECT_ID` nhận project ID dạng số hoặc đường dẫn namespace/project;
API sẽ URL-encode giá trị. `GIT_TARGET_BRANCH` phải là branch có trên GitLab.
Lệnh chạy không chấp nhận tham số Ansible tùy ý từ Telegram. Để debug, thực
hiện thủ công trên cùng commit và cùng môi trường; bot chỉ trả về `PLAY RECAP`
để tránh gửi nội dung task/secret ra Telegram.

## Kiểm tra

```bash
python -m unittest discover -s tests -v
```

Test dùng repo Git local và executable Ansible giả để xác nhận đúng argv,
chỉ tạo MR sau exit 0, và từ chối branch không có diff. Test không kết nối
GitLab thật hay Ansible server thật.
