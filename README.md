# infra-bot

Telegram bot tự động hoá whitelist IP / Zero Trust cho GPay (Cloudflare + Terragrunt).

## Cấu trúc

| Repo | Nội dung |
|------|----------|
| `developer/infra-bot` (repo này) | Source code + `Dockerfile`. Jenkins build image từ đây. |
| `applications/charts/devops/infra-bot` | Helm chart deploy. Jenkins cập nhật `devops.infra-bot.version` trong `values-<env>.yaml`. |

## Build image (như các service khác)

- Jenkins job: project `devops`, application `infra-bot` (đã khai báo trong `jenkins-seed-jobs/jobs.yaml`).
- Branch `deploy-dev` / `deploy-sandbox` / `deploy-staging` → build cho env tương ứng.
- Image: `docker-hub-internal.g-pay.vn/development/devops/infra-bot:<git-describe>`
- Cần tag `v1.0.0` để `git describe` sinh version hợp lệ (giống `support-agent`):

```
git tag v1.0.0
git push origin v1.0.0
```

## Chạy local

```
pip install -r requirements.txt
python main.py
```

Các biến môi trường lấy từ Secret của chart (xem `applications/charts/devops/infra-bot/templates/secret.yaml`)