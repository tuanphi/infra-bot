# Ansible Telegram bot

Bot hỗ trợ `/gitlab` để nhập namespace/service/user, chọn role và xác nhận
Yes/No trước khi cập nhật `group_vars/gitlab-ghub` trên branch `master`.
Bot gửi toàn bộ Git status/diff và chạy
`ansible-playbook -i nonprod gitlab-repos-ghub.yaml --tags=<namespace>,project_user_access`
khi kiểm tra hợp lệ. Sau Ansible thành công, bot hiện thêm hai nút:

- **Yes:** tạo branch/commit, push và tìm/tạo Merge Request vào `master`.
- **No:** phục hồi YAML trước thay đổi và chạy lại playbook với cấu hình cũ.

Cả hai lựa chọn đều kết thúc bằng `git switch master` và
`git pull --ff-only origin master`. Xem
[hướng dẫn luồng /gitlab](GITLAB-REPO-ROLE-FLOW.md) để biết kiểm tra Git,
trạng thái phiên và cách thử lại khi lỗi.

Luồng `/run <source-branch>` chạy lệnh dưới đây từ thư mục gốc của source
`gitlab.g-pay.vn/devops/ansible/infra`, sau đó tạo GitLab Merge Request khi
Ansible thành công:

```bash
ansible-playbook -i nonprod gitlab-repos.yaml --tags=project_user_access
```

Telegram hỗ trợ `/gitlab`, `/cancel`, `/run <source-branch>`, `/whoami`, `/id`, `/start` và `/help`.
`/whoami` cho biết ID của người gửi và chat để đối chiếu `ALLOWED_USER_IDS`/
`ALLOWED_GROUP_ID`. Tin nhắn riêng cần `ALLOW_PRIVATE_CHAT=true` và user được
whitelist; mặc định chỉ dùng trong nhóm đã cấu hình.
Với `/run`, source branch **phải được commit/push từ trước**
và có diff so với `GIT_TARGET_BRANCH`. Chạy một playbook hiện có không tự tạo
diff cho MR. `/run` dùng source đã commit; `/gitlab` sửa membership trong file
`group_vars/gitlab-ghub` theo cấu trúc đã cung cấp. GitLab repo không truy cập được trong môi
trường thiết kế này, do đó cần kiểm tra inventory `nonprod`, vị trí playbook
và các dependency role/collection tại môi trường của bạn.

## Luồng /gitlab

1. Gửi `/gitlab`, nhập namespace → service → username GitLab, rồi chọn một
   role: `maintainer`, `developer`, `reporter`, `guest`.
2. Bot đọc role hiện tại từ YAML và hỏi xác nhận Yes/No. No hoặc `/cancel`
   tại bước này kết thúc trước khi sửa file hoặc chạy Ansible.
3. Yes: bot kiểm tra repo `master` sạch, sửa đúng membership trong
   `group_vars/gitlab-ghub`, gửi Git status/diff và chạy playbook Ghub.
4. Ansible exit 0: bot gửi kết quả/PLAY RECAP và menu:

   ```text
   Yes: Tạo merge request đẩy lên nhánh master / No: Huỷ thay đổi
   ```

5. Yes cuối: tạo branch theo UTC+7, commit riêng file YAML, push và tìm/tạo
   MR vào `master`. Tên branch và tiêu đề MR:

   ```text
   <year>-<month>-<day>-<hour>-<minute>/<namespace>-<service>
   2026-10-08-13-30/ghub-website-merchant-portal-client
   ```

   Commit message:

   ```text
   gitlab-repo: Grant role <role> for <user> to repo <namespace>/<service>
   ```

6. No cuối: phục hồi chính xác YAML trước khi sửa, rồi chạy lại
   `ansible-playbook -i nonprod gitlab-repos-ghub.yaml
   --tags=<namespace>,project_user_access` để áp dụng cấu hình cũ.
7. Hoàn tất lựa chọn: switch `master`, pull `--ff-only`, gửi kết quả và nhả khóa.

MR được tạo trong repo Ansible do `GITLAB_PROJECT_ID` xác định. MR không tự
merge; YAML mới vào `master` sau khi merge. Switch về `master` sau Yes không
chạy lại playbook và không huỷ quyền đã áp dụng. Nên merge MR trước lượt thay
đổi tiếp theo cho cùng service để preview từ YAML phản ánh cấu hình đã chấp nhận.

Khóa dùng chung với `/run` được giữ cả lúc đang chờ quyết định cuối. `/gitlab`
hoặc `/cancel` của người mở phiên hiển thị lại menu khi đang chờ, hoặc báo đang
xử lý nếu backend chưa xong. Nút của phiên cũ hoặc người khác không có hiệu lực.

Nếu Ansible lần đầu lỗi/timeout, bot giữ diff để kiểm tra và kết thúc phiên vì
quyền có thể đã áp dụng một phần. Nếu lỗi khi hoàn tất Yes/No, bot giữ phiên,
khóa và trạng thái từng bước để thử lại. Sau khi lựa chọn đã bắt đầu được xử
lý, bot chỉ hiện nút tương ứng; không tạo lại commit đã có hoặc MR đang mở khi
thử lại. Nhánh đã push vẫn được thử switch/pull về `master` kể cả khi API MR lỗi.

No phục hồi YAML và chạy lại playbook; quyền thực tế chỉ trở về trạng thái cũ
khi playbook xử lý cả thu hồi membership dư và phục hồi role. Cần kiểm tra
trường hợp user ban đầu chưa có quyền. Phiên nằm trong bộ nhớ và mất khi
restart; cần kiểm tra checkout, branch/MR và quyền nếu lượt bị gián đoạn.

## Luồng /run

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
tính idempotent. Bot chỉ cho phép một lượt xử lý tại một thời điểm trong một
process, bao gồm lúc `/gitlab` đang chờ Yes/No cuối; triển khai đúng **một replica**
khi dùng Telegram polling.

## Khởi động và HTTP health

Sau khi runner chuẩn bị SSH key, bot đọc `/mnt/secrets/.env`, kiểm tra cấu hình
và clone repo Ansible vào `/app/infra` trước khi mở HTTP health và Telegram polling.
Đặt các biến sau trong file `.env`:

```dotenv
TELEGRAM_TOKEN=replace-with-telegram-token
ALLOWED_GROUP_ID=-1001234567890
ALLOWED_USER_IDS=123456789
GIT_REPO_URL=https://gitlab.g-pay.vn/devops/ansible/infra
GITLAB_URL=https://gitlab.g-pay.vn
GITLAB_PROJECT_ID=devops/ansible/infra
GITLAB_USERNAME=tuanpv
GITLAB_TOKEN=replace-with-gitlab-token
GIT_TARGET_BRANCH=master
GIT_CLONE_DIR=/app/infra
GIT_CLONE_TIMEOUT_SECONDS=180
HEALTH_PORT=8080
```

`GIT_CLONE_DIR`, `GIT_CLONE_TIMEOUT_SECONDS` và `HEALTH_PORT` có các giá trị mặc
định như ví dụ, nên không bắt buộc khai báo. `GITLAB_USERNAME` bắt buộc khi
`GIT_REPO_URL` dùng HTTPS; `GITLAB_TOKEN` hiện có được dùng làm password.
Bot dựng URL clone từ ba biến này, URL-encode username/token nếu có ký tự đặc biệt,
và thực hiện lệnh tương ứng:

```bash
git clone --no-tags --single-branch --branch master -- \
  "https://tuanpv:<GITLAB_TOKEN>@gitlab.g-pay.vn/devops/ansible/infra" /app/infra
```

`<GITLAB_TOKEN>` được thay bằng giá trị thật trong Python, không chạy qua shell.
Sau khi clone, bot đặt lại `remote.origin.url` về URL không chứa credential.
Các lệnh fetch/ls-remote/pull/push dùng Git credential helper với cùng `GITLAB_USERNAME`
và `GITLAB_TOKEN`; helper chỉ trả credential cho đúng host và đường dẫn repo.
Token cần quyền đọc/push repo Ansible và quyền API tìm/tạo MR trong project đó.
Giữ xác minh TLS; CA nội bộ phải có trong trust store hoặc cấu hình Git bằng
`GIT_SSL_CAINFO` trỏ đến file CA đã mount.

Khi Git thất bại, thông báo chứa exit code và phần lỗi chi tiết của Git đã che
token/credential. Exit 128 chưa đủ để xác định lỗi xác thực: log mới giúp phân
biệt `Authentication failed`, lỗi certificate, DNS/network hoặc branch không tồn tại.

Nếu checkout đã tồn tại, bot kiểm tra origin và working tree, fetch branch rồi
checkout branch `GIT_TARGET_BRANCH`, cập nhật bằng merge `--ff-only` từ
`origin/GIT_TARGET_BRANCH`. Nếu thư mục có
dữ liệu khác, origin không khớp hoặc có thay đổi local, bot dừng khởi động và giữ
nguyên dữ liệu. Clone/fetch lỗi hoặc vượt timeout cũng dừng khởi động.

`GET /health` và `HEAD /health` trả HTTP **200**; các path khác trả **404**.
HTTP dùng thư viện chuẩn Python, lắng nghe trên `0.0.0.0:8080` mặc định và chạy
trong thread riêng để vẫn nhận probe khi Ansible đang chạy. Response GET:

```json
{"status":"ok"}
```

Health mở sau khi clone thành công. Nếu dùng startup/liveness probe, dành đủ
thời gian cho bước clone lúc khởi động. HTTP server đóng khi polling kết thúc.
Mỗi `/run` vẫn dùng checkout tạm riêng để giữ commit đang chạy và `/app/infra`
không bị chuyển sang source branch của một lượt Ansible.

## Runtime

`Dockerfile` dùng Python `3.8.10`, cài `ansible-core==2.12.10`,
`Jinja2==2.10.1`, `MarkupSafe==2.0.1` và xác nhận version khi build.
MarkupSafe được pin vì Jinja cũ cần `soft_unicode`. Ảnh base cũ cần được
mirror/nâng cấp theo chính sách bảo mật riêng nếu đem dùng lâu dài.

```bash
docker build -t infra-ansible-bot:health-startup .
docker run --rm --name infra-bot \
  -p 8080:8080 \
  -v /home/tuanpv/.ssh/id_ed25519:/mnt/secrets/git_private.pem:ro \
  -v /home/tuanpv/.ssh/known_hosts:/mnt/secrets/known_hosts:ro \
  -v /home/tuanpv/g-pay/zerotrust-alert/.env:/mnt/secrets/.env:ro \
  infra-ansible-bot:health-startup

curl -i http://localhost:8080/health
```

Tạo `.env` theo cấu hình trên, điền token và ID thật; **không commit `.env` hoặc
private key**. `known_hosts` phải chứa host key đã xác minh của GitLab server. Nếu GitLab dùng CA nội bộ, đưa CA vào trust store của container.

Bot dùng `load_env_file()` trong `config.py` để đọc `/mnt/secrets/.env` trước
`Settings.from_env()`. Hàm chỉ dùng thư viện chuẩn Python (`os`, `re`, `pathlib`),
không cần `dotenv`, `--env-file` của Docker hoặc `envFrom` của Kubernetes.
Runner chép SSH key và `known_hosts` vào `/root/.ssh`, đặt quyền `0600`, rồi
chạy Python; các file mount vẫn chỉ đọc.

Định dạng `.env` hỗ trợ một biến `KEY=value` trên mỗi dòng, dòng trống, comment
ở đầu dòng (`#`), từ khóa `export` tùy chọn và cặp nháy đơn/đôi bao quanh giá trị.
Giá trị được giữ nguyên, không nội suy `${VAR}`, không giải mã escape, không
hỗ trợ giá trị nhiều dòng hoặc inline comment. Đặt comment trên dòng riêng;
dấu `#` trong giá trị được giữ nguyên. UTF-8 BOM và CRLF được chấp nhận.
Biến môi trường đã tồn tại được ưu tiên (`override=False`); key lặp trong file
lấy giá trị cuối cùng. File sai cú pháp dừng khởi động trước khi thay đổi env;
thông báo chỉ chứa đường dẫn và số dòng, không chứa giá trị.

Env được nạp chỉ thuộc tiến trình bot và các tiến trình con mà bot tạo,
bao gồm Git/Ansible. `docker exec env` chạy một tiến trình khác nên không hiển
thị những biến được nạp riêng bên trong bot. Đổi `.env` cần khởi động lại bot.

GitLab token cần quyền API tìm/tạo MR và đọc/push repo qua HTTPS; Ansible SSH key
cần kết nối đúng host nonprod. Nếu dùng URL Git SSH, key cũng cần quyền đọc/push repo.
Nếu playbook dùng Ansible Vault, bổ sung secret
và `ANSIBLE_VAULT_PASSWORD_FILE` vào runtime. Nếu repo khai báo roles/collections,
cài đúng phiên bản theo repo trước khi chạy.

`GITLAB_PROJECT_ID` nhận project ID dạng số hoặc đường dẫn namespace/project;
API sẽ URL-encode giá trị; đặt ID/path của repo Ansible `devops/ansible/infra`,
không phải project service được cấp quyền. Với `/gitlab`, đặt
`GIT_TARGET_BRANCH=master`; branch này phải tồn tại trên GitLab.
Lệnh chạy không chấp nhận tham số Ansible tùy ý từ Telegram. Để debug, thực
hiện thủ công trên cùng commit và cùng môi trường; bot chỉ trả về `PLAY RECAP`
để tránh gửi nội dung task/secret ra Telegram.

## Kiểm tra

Thay đổi luồng Yes/No sau Ansible đã qua 16 kiểm tra local với repo Git thật,
executable Ansible giả và API GitLab/Telegram mô phỏng. Bao gồm commit/MR đúng
mẫu, phục hồi YAML trước Ansible, lỗi push/MR/pull và thử lại, bảo vệ thay đổi
ngoài dự kiến, quyền/nút Telegram, khóa phiên và cú pháp Python 3.8.

Tại container, kiểm tra cú pháp các module:

```bash
python -m py_compile bot.py config.py git_auth.py runtime.py workflow.py gitlab_access.py gitlab_conversation.py
```

Kiểm tra tích hợp trên service thử nghiệm: hai bước Yes/No, quyền trước/sau No,
branch/commit/MR sau Yes, `master` sạch và đã pull, nút cũ/người khác, chặn `/run`
khi chờ quyết định và thử lại khi lỗi. Các bước cụ thể nằm trong
[hướng dẫn kiểm chứng luồng /gitlab](GITLAB-REPO-ROLE-FLOW.md#kiểm-chứng).
Kiểm tra thêm `/run <source-branch>` để xác nhận luồng source đã commit vẫn
chạy đúng. Kiểm tra local không thay thế xác minh quyền trên GitLab thật.
