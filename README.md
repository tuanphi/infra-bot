# Ansible Telegram bot

Bot hỗ trợ `/gitlab` để gom nhiều lượt phân quyền: chọn **ghub/non-ghub**, namespace/service bằng nút,
nhập một hoặc nhiều username GitLab và chọn role chung. Bot đối chiếu file YAML của nhóm đã
chọn với quyền thực tế qua GitLab REST API: đã khớp thì kết
thúc; YAML đúng nhưng API khác thì hỏi Yes/No để chạy Ansible và xác minh lại
API, không tạo commit/branch. Khi YAML cần thay đổi, bot hỏi:

> ❓ Bạn có muốn tiếp tục thêm quyền user
> Yes: Thêm lượt /gitlab mới / No: Chạy Ansible cho toàn bộ phiên

**Yes** lưu thay đổi YAML của lượt hiện tại, giữ các lượt trước rồi mở lại menu
chọn ghub/non-ghub. **No** cũng lưu lượt hiện tại và bắt đầu Ansible cho tất cả
các lượt đã gom. Chưa có playbook nào chạy trong lúc chọn Yes để thêm lượt.

Sau khi Ansible thành công, luồng sửa YAML vẫn có hai lựa chọn:

- **Yes:** tạo một branch, mỗi lượt có diff YAML tạo một commit riêng; xem toàn bộ difference và xác nhận push.
- **No:** phục hồi YAML ban đầu của cả phiên, chạy lại các namespace/service đã chọn theo cấu hình cũ.

| Nhóm | File YAML | Playbook của /gitlab |
| --- | --- | --- |
| ghub | `group_vars/gitlab-ghub` | `gitlab-repos-ghub.yaml` |
| non-ghub | `group_vars/gitlab` | `gitlab-repos.yaml` |

Cả hai lựa chọn đều kết thúc bằng `git checkout master` và
`git pull --ff-only origin master`. Xem
[hướng dẫn luồng /gitlab](GITLAB-REPO-ROLE-FLOW.md) để biết kiểm tra Git,
trạng thái phiên và cách thử lại khi lỗi.

Tại menu **Thông tin branch trước khi push**, người dùng xem branch, difference,
commit SHA và commit message. **Yes** mới push branch lên `origin`;
**No** chuyển sang cùng luồng phục hồi YAML/chạy lại Ansible. Các lệnh chuyển
nhánh dùng `checkout` để tương thích Git chưa hỗ trợ `switch`.

Menu BotCommand gồm `/gitlab`, `/cancle`, `/reset`, `/help` và được đăng ký tự động khi bot khởi động.
`/cancel` là alias của `/cancle`; `/whoami`, `/id`, `/start` vẫn dùng được.
Luồng `/run` và mã chạy branch/tạo MR riêng đã được gỡ.
`/whoami` cho biết ID của người gửi và chat để đối chiếu `ALLOWED_USER_IDS`/
`ALLOWED_GROUP_ID`. Tin nhắn riêng cần `ALLOW_PRIVATE_CHAT=true` và user được
whitelist; mặc định chỉ dùng trong nhóm đã cấu hình.
Bot đọc cấu hình service từ YAML. Cần kiểm tra inventory `nonprod`, vị trí
playbook và các dependency role/collection tại môi trường triển khai.

## Menu lệnh và quote Telegram

`bot.py` định nghĩa `BOT_COMMANDS` và gọi `set_my_commands()` qua `post_init`,
cập nhật menu mặc định và menu riêng của `ALLOWED_GROUP_ID`:

| Lệnh | Mô tả |
| --- | --- |
| `/gitlab` | 🔐 Phân quyền repo GitLab |
| `/cancle` | 🛑 Huỷ các lượt đang gom trước khi chạy Ansible |
| `/reset` | ♻️ Lấy master và đồng bộ quyền namespace |
| `/help` | ❓ Hướng dẫn sử dụng bot |

`/cancle` và alias `/cancel` cùng dùng handler `cancel()`: huỷ toàn bộ YAML đang gom trước Ansible;
đang chạy thì báo chờ; đang chờ quyết định/push thì hiển thị lại menu để hoàn tất.
Bot chỉ có một khóa repo trong một process; triển khai một replica khi polling.

`reply_full()` gửi output trong quote HTML `<blockquote>...</blockquote>`:
Git status, Git diff, thông tin quyền, kết quả Ansible/PLAY RECAP/task timing,
kết quả `/reset`, thông tin branch/commit/difference, lỗi và kết quả push/phục hồi.
Mỗi phần có tối đa 3500 đơn vị UTF-16 sau khi hiển thị, chia trước khi escape
`<`, `>`, `&` và dấu nháy; nội dung dài được gửi đầy đủ qua nhiều quote.
Các menu có nút và câu hỏi Yes/No vẫn dùng tin nhắn thường.
Không cần nâng phiên bản `python-telegram-bot==20.7` cho HTML quote.
Xem [Telegram HTML formatting](https://core.telegram.org/bots/api#html-style).

## Luồng /gitlab

1. `/gitlab` → chọn ghub/non-ghub → **📁 Chọn project** → **📁 Chọn service**.
   Các menu đọc danh sách từ YAML, 12 lựa chọn/trang và hai nút/hàng.
2. Nhập một hoặc nhiều username (dấu phẩy, khoảng trắng hoặc xuống dòng), tối đa 50 user/lượt.
   Chọn role chung: maintainer, developer, reporter hoặc guest.
3. Bot đọc GitLab REST API, hiển thị thông tin quyền với icon và đối chiếu YAML.
   YAML/API đều đúng và chưa có lượt trước: ✅ đã có quyền, kết thúc.
4. Khi cần sửa YAML, bot gửi quote **📝 Sẽ cập nhật YAML và chạy Ansible.**,
   sau đó hỏi **❓ Bạn có muốn tiếp tục thêm quyền user**, hai nút Yes/No.
   Khi chỉ đồng bộ API, bot dùng thông báo 🔄 tương ứng và cùng menu gom lượt.
5. **Yes:** kiểm tra snapshot và lưu YAML; các diff đã gom vẫn được giữ. Mở lại
   menu ghub/non-ghub để thêm namespace/service/user/role khác. API và YAML
   của lượt mới được kiểm tra trên dữ liệu đã sửa của các lượt trước.
6. **No:** lưu lượt hiện tại, chạy Ansible theo dữ liệu YAML cuối cùng của cả phiên.
   Các lượt cùng nhóm/namespace được gom thành một lần chạy với đúng các service
   đã chọn. Namespace khác hoặc nhóm khác dùng lần chạy/playbook tương ứng.
   `group_vars/gitlab-bot` truyền bằng `--extra-vars`, được xoá sau mỗi lần chạy.
7. Với các lượt `sync`, bot đọc lại API xác nhận role; nếu lượt sau đổi role của
   cùng user/project thì role ở lượt sau là giá trị cần xác nhận. Phiên chỉ có
   `sync`/`unchanged` kết thúc mà không tạo branch/commit.
8. Có diff YAML và Ansible thành công: Yes chuẩn bị branch/commit local; chưa push.
   Mỗi lượt sửa YAML tạo **một commit** chứa đúng snapshot của lượt đó. Lượt không
   đổi YAML không tạo commit rỗng. Một lượt nhập nhiều user vẫn tạo một commit.

   | Số lượt đã gom | Branch |
   | --- | --- |
   | 1 | `bot-YYYYMMDD-HHMMSS/<namespace>/<service>` |
   | Nhiều hơn 1 | `bot-YYYYMMDD-HHMMSS/Update-permission` |

   Thời gian dùng UTC+7. Commit message giữ định dạng:

   ```text
   gitlab-repo: Grant role <role> for <user1>, <user2> to repo <namespace>/<service>
   ```

9. Quote **🔎 Thông tin branch trước khi push** chứa branch, commit và toàn bộ
   commit message/difference. **✅ Yes** tại menu push mới đẩy branch lên origin.
   **❌ No** khôi phục cả phiên. Kết thúc bằng `git checkout master` và
   `git pull --ff-only origin master`; bot không tạo MR.

Trong lúc gom lượt, `/gitlab` mở lại menu và giữ các YAML đã lưu; `/cancle`
(alias `/cancel`) phục hồi toàn bộ YAML ban đầu, không chạy Ansible, nhả khóa.
`/reset` và user khác không được chiếm repo đang thuộc phiên này.
Sau khi bắt đầu Ansible, `/cancle` hiển thị menu đang chờ hoặc báo đang xử lý.
No trong menu gom lượt có nghĩa **chạy Ansible**, khác No sau Ansible/menu push
có nghĩa **huỷ thay đổi**.

YAML đã đúng nhưng API khác: chạy Ansible, xác minh lại API; không diff/commit
cho lượt đó. Nếu YAML/API đã đúng ở một lượt mới trong phiên đang gom, bot báo
✅ và giữ các lượt trước để tiếp tục hoặc chạy cả phiên.

User ID được kiểm tra lại trước khi chạy Ansible hoặc phục hồi quyền. Repo phải
ở cùng master/commit trong lúc gom; chỉ các file YAML và nội dung do phiên này
đã chuẩn bị được phép thay đổi. File staged hoặc sửa ngoài phiên làm luồng dừng
để kiểm tra, không ghi đè dữ liệu người vận hành.

Ansible lỗi sau khi có diff YAML: giữ toàn bộ phiên và khóa; Yes chạy lại Ansible
cho cả phiên, No phục hồi YAML/quyền cũ. Nếu chuẩn bị commit lỗi giữa chừng,
retry Yes tiếp tục từ commit chưa tạo và không lặp commit cũ. Khi đã xác nhận
push, chỉ retry Yes để hoàn tất push/checkout/pull.

Các kết quả Git, quyền, Ansible và branch/difference đều dùng quote HTML; menu
nút dùng text thường. Phiên được giữ trong RAM, không tự phục hồi sau restart.
Triển khai một replica khi polling. Xem [luồng chi tiết](GITLAB-REPO-ROLE-FLOW.md).

## Luồng /reset

1. Gửi `/reset` → chọn **ghub/non-ghub** bằng nút.
2. Chọn namespace bằng nút; danh sách dài có nút chuyển trang.
3. Bot giữ khóa chung với `/gitlab`, chạy `git checkout master` rồi
   `git pull --ff-only origin master`. Repo phải sạch; bot giữ các diff chưa
   hoàn tất để người vận hành xử lý. Local master phải khớp origin/master.
4. Đọc YAML sau pull, tạo `group_vars/gitlab-bot` chỉ có namespace đã chọn và chạy:

   ```bash
   ansible-playbook -i nonprod gitlab-repos.yaml --tags=<namespace>,project_user_access --extra-vars=@group_vars/gitlab-bot
   ```

   `/reset` dùng `gitlab-repos.yaml` cho **cả ghub và non-ghub**. Dữ liệu ghub
   lấy từ `group_vars/gitlab-ghub`; non-ghub lấy từ `group_vars/gitlab`.
5. Xoá file tạm, gửi ✅/❌ và thời gian chạy, xoá phiên và nhả khóa. `/reset`
   áp dụng quyền theo YAML master, không sửa YAML hoặc tạo branch/commit/MR.
   Ansible exit 0 là kết quả playbook; bước này không đọc lại API cho mọi user.

Chọn namespace bắt đầu chạy ngay. `/cancle` dừng trước bước này; khi Ansible
đang chạy, bot yêu cầu chờ. Nếu `/gitlab` đang chờ Yes/No, `/reset` hiển thị lại
menu của lượt đó để hoàn tất trước.

## Giới hạn dữ liệu và đo thời gian Ansible

Các lượt `/gitlab`, huỷ thay đổi bằng No và `/reset` đều tạo file tạm
`group_vars/gitlab-bot` từ YAML đang áp dụng. Các biến dùng chung được giữ;
`gitlab_groups` và `gitlab_projects` được lọc đúng namespace. Bot truyền file
bằng `--extra-vars` và đặt `ANSIBLE_HASH_BEHAVIOUR=replace` cho subprocess để
các namespace khác không bị gộp lại từ inventory/vars_files.

`/gitlab` và No sau Ansible lọc các service đã gom trong từng namespace; `/reset` giữ toàn bộ project
trong namespace. Với file non-ghub đã cung cấp, `devops` có 9 project trong
tổng 258: một lượt chọn một service truyền 1 project; nhiều lượt truyền tập service
đã chọn trong namespace, còn `/reset` chọn devops truyền đủ 9 project.
Namespace `qc` không có parent vẫn dùng được.
File tạm có quyền 0600, không được commit, được xoá sau success, failure và timeout.
YAML chính vẫn được cập nhật bởi luồng `/gitlab` hiện có: thay đúng membership,
giữ các namespace khác và giữ diff để xác nhận push. File non-ghub chính là
`group_vars/gitlab`; ghub là `group_vars/gitlab-ghub`.

Kết quả có tổng thời gian Ansible, số project đã truyền và tối đa 5 task tốn
thời gian nhất. Callback `ansible_callbacks/bot_timing.py` dùng monotonic clock
đo thời gian giữa các sự kiện bắt đầu task và khi kết thúc playbook; bao gồm
handler và toàn bộ vòng lặp của task, không phải thời gian riêng của từng API.
Runner chỉ bật callback cho subprocess của `/gitlab`, No và `/reset` qua
`ANSIBLE_CALLBACK_PLUGINS`/`ANSIBLE_CALLBACKS_ENABLED`; giữ callback đã cấu hình.
Dockerfile copy và kiểm tra cú pháp file này khi build. Task có `no_log` được
hiển thị bằng tên `task (no_log)`.
Ví dụ định dạng: `⏱ Ansible: <giây>; namespace devops: 9/258 project.`
Chưa đo playbook/GitLab nội bộ nên không cam kết thời gian dưới 5 phút.

`--tags=a,b` chọn task khớp một trong hai tag, không tự giới hạn namespace
trong dữ liệu vòng lặp. Tăng `forks` chủ yếu tăng số host song song; với một
localhost, cần giảm dữ liệu vòng lặp để giới hạn số project phải xử lý.
Xem [Ansible variable precedence](https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_variables.html#variable-precedence-where-should-i-put-a-variable)
và [callback plugins](https://docs.ansible.com/projects/ansible-core/2.15/plugins/callback.html).

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
Token cần quyền đọc/push repo Ansible, API đọc
user/project service/membership để kiểm tra role. API Python cần CA được tin cậy
trong Python/container; `GIT_SSL_CAINFO` chỉ áp dụng cho Git.
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

Bot đọc `/mnt/secrets/.env` trực tiếp trong `main()` của `bot.py`, dùng thư
viện chuẩn Python; các biến env có sẵn được ưu tiên. Không có module cấu hình
riêng hoặc class `Settings`. Cấu hình cần cho bot/Git/Ansible được gom bằng
`SimpleNamespace`, với phép chuyển kiểu cần thiết cho ID, timeout và port.

Định dạng file: một `KEY=value` mỗi dòng, bỏ dòng trống/comment đầu dòng,
hỗ trợ `export` và cặp nháy đơn/đôi; không nội suy biến hoặc giá trị nhiều dòng.
UTF-8 BOM và CRLF được chấp nhận. Không cần thêm dependency `dotenv`.
Runner vẫn chép SSH key và `known_hosts` vào `/root/.ssh`, đặt quyền `0600`;
các file mount chỉ đọc.

Env được nạp chỉ thuộc tiến trình bot và các tiến trình con mà bot tạo,
bao gồm Git/Ansible. `docker exec env` chạy một tiến trình khác nên không hiển
thị những biến được nạp riêng bên trong bot. Đổi `.env` cần khởi động lại bot.

GitLab token cần quyền API đọc user/project/membership và đọc/push repo qua HTTPS; Ansible SSH key
cần kết nối đúng host nonprod. Nếu dùng URL Git SSH, key cũng cần quyền đọc/push repo.
Nếu playbook dùng Ansible Vault, bổ sung secret
và `ANSIBLE_VAULT_PASSWORD_FILE` vào runtime. Nếu repo khai báo roles/collections,
cài đúng phiên bản theo repo trước khi chạy.

API kiểm tra role dùng đầy đủ `project.path` lấy từ YAML, ví dụ
`development/ghub-website/merchant-portal-client`. Với `/gitlab`, đặt
`GIT_TARGET_BRANCH=master`; branch này phải tồn tại trên GitLab.
Lệnh chạy không chấp nhận tham số Ansible tùy ý từ Telegram. Để debug, thực
hiện thủ công trên cùng commit và cùng môi trường. Bot trả `PLAY RECAP`,
thời gian chạy Ansible và số project; không gửi toàn bộ stdout Ansible.

## Kiểm tra

Bộ kiểm tra có **77 test**: 47 test input/backend/hội thoại đơn, 19 test gom lượt,
commit riêng, rollback, retry và lỗi API/Telegram; 4 test BotCommand/quote, 2 test
startup/branch và **5 test chạy Ansible thật trên localhost**. Git dùng các repo
bare tạm; API và Telegram được mô phỏng, không gọi GitLab nội bộ.

Các test Ansible thật kiểm tra ưu tiên extra-vars, giới hạn namespace/service,
callback timing, gom hai service trong một namespace và cả ghub/non-ghub.
Môi trường: Python 3.8.20, ansible-core 2.12.10, Jinja2 2.10.1.

```bash
python -m unittest discover -s tests -v
```

Tại container, kiểm tra cú pháp các module:

```bash
python -m py_compile bot.py git_auth.py runtime.py workflow.py gitlab_access.py gitlab_conversation.py ansible_callbacks/bot_timing.py
```

Kiểm tra trên service thử nghiệm: Yes giữ YAML nhưng chưa chạy Ansible; No
chạy đủ các lượt; branch nhiều lượt có đúng các commit nối tiếp và toàn bộ diff.
Kiểm tra cả hai nhóm, rollback toàn bộ, API sau sync và checkout/pull sau push.
Chi tiết ở [hướng dẫn kiểm chứng](GITLAB-REPO-ROLE-FLOW.md#kiểm-chứng).