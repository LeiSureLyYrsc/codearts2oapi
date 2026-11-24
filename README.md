# codearts2api

华为云中国站（huaweicloud.com）**码道 CodeArts Agent** 到 **OpenAI 兼容 API** 的本地网关。

技术栈：Python 3.12、uv、FastAPI。上游认证使用华为云官方 **SDK-HMAC-SHA256** 签名算法。凭证放在**多账号池**中，每个账号可以是：

1. **官方客户端同款浏览器 SSO / OAuth**（推荐，不必粘贴长期 AK/SK）
2. 你自己的华为云 IAM **永久 AK/SK**（以及可选的 STS `X-Security-Token`）

账号认证与运行配置完全分开：`data/config.json` 只负责应用设置，缺失时由项目自动创建，无需复制模板。每个账号单独保存在 `data/accounts/<id>.json`（包含 AK/SK 或 OAuth 会话）；请在 WebUI「账号池」里添加，或导入 Auth 文件。不要把账号密钥写进 config。

时区、签到 cron、调度开关、账号池策略、上游超时等运行配置同样写在 `data/config.json`，也可在 WebUI 里修改。`.env` 只保留：启动用的 `CA2A_API_KEY`（随后可在 WebUI 更换并持久化）、仅能通过环境变量设置的监听地址 `CA2A_HOST` / `CA2A_PORT`，以及可选的数据目录 `CA2A_DATA_DIR` / `CA2A_AUTH_DIR`。

对话请求按配置的策略在启用账号之间轮换，并在上游返回 401/403/429/5xx 时把该账号放入冷却并改用其他账号（只做故障转移，不是插队或绕过限流）。每日签到和权益领取会**分别对每个已启用账号**执行。

项目内支持官方「每日签到得积分」活动、进程内定时签到，以及动态拉取模型列表。本仓库全部为原创实现，没有 fork 或拷贝其他网关项目。

## 分析与实现依据（仅官方材料）

本项目只依据华为云官方文档，以及从华为云官方下载渠道取得的官方客户端安装包中的配置/内嵌接口常量。公开 API 目录没写清的路径，若在官方客户端中能核对到，会在下文标明「来自官方客户端」；若官方文档和官方客户端都无法核实，则只留 hook，不自行编造。

### 官方文档

- [码道 API 概览](https://support.huaweicloud.com/api-codeartsagent/codeartsagent-api-0002.html)：公开目录目前只有用量 / commit / 代码生成同步，**不包含**对话、模型列表或签到接口。
- [每日签到得积分](https://support.huaweicloud.com/offers-codeartsagent/codeartsagent_offers_0004.html)：活动时间 2026-10-27 20:00:00 至 2026-12-31 23:59:59（UTC+8）；体验版 100、标准版 150、高级/旗舰 200 积分，当日有效；仅已升级为积分计费的个人版用户可参与。
- [API 签名指南（机制）](https://support.huaweicloud.com/devg-apisign/api-sign-algorithm-001.html) 与 [Python 签名指导](https://support.huaweicloud.com/devg-apisign/api-sign-sdk-python.html)：SDK-HMAC-SHA256。
- [系统内置模型配置](https://support.huaweicloud.com/usermanual-codeartsagent/codeartsagent_ug_0072.html)：官方用户指南中的内置模型说明（如 GLM-5.2）。
- [码道 IDE 快速启动 / 登录](https://support.huaweicloud.com/usermanual-codeartsagent/codeartsagent_ug_0002.html)：客户端会跳转外部浏览器打开华为云登录页，用户登录后在授权页「确认授权」。
- [码道 CLI 授权](https://support.huaweicloud.com/usermanual-cli/codeartsagent_cli_0026.html)：官方提供「网页端登录授权」和「AK/SK 鉴权授权」两种方式；浏览器未自动打开时可复制终端中的登录链接。
- [配置账号 30 天免登录](https://support.huaweicloud.com/usermanual-enterprise/codeartsagent_enterprise_0013.html)：IAM 用户相关权限为 `signin::authorizeOAuth2Access`、`signin::createOauth2Token`。
- [配置 SSO 登录](https://support.huaweicloud.com/usermanual-enterprise/codeartsagent_enterprise_0019.html)：企业联邦用户的 SAML/OIDC 控制台 SSO（与本网关实现的官方客户端浏览器授权不是同一条路径）。
- IAM 访问密钥：<https://console.huaweicloud.com/iam/#/mine/accessKey>
- 官方下载页：<https://codearts.huaweicloud.com/download.html>
- 官方门户：<https://codearts.huaweicloud.com/portal>

### 官方客户端（已核对的安装包）

为核实公开目录未收录的登录与网关地址，本项目查看了官方码道 CLI 安装脚本与 26.11.11 Linux x64 安装包（官方 OBS，不是第三方镜像）：

- `https://cnnorth4-cloudide-marketplace.obs.cn-north-4.myhuaweicloud.com/codearts/cli_tui/install_script/install.sh`
- `https://codearts-agent-obs-cdn.huaweicloud.com/codearts/cli_tui/latest/codearts-install-26.11.11-linux-x64.tar.gz`

从该官方 `agentkernel` 中核对到的中国站生产常量与行为（见下文「OAuth / SSO」和「opengw 权益」）。**官方 IDE / 插件安装包体积很大，本次未完整解包核对其 client_id**；若你使用的是 IDE 而不是 CLI，其 `client_id` 可能不同，可在 `data/config.json` 的 `settings.oauth_client_id` 或 WebUI 中覆盖。默认值 `CodeArts_Tui` 来自官方 CLI。

另用 Windows 版 CodeArts 桌面端 0.2.9 的 `resources/product.json` 和 `resources/app.asar` 核对了每日活动、内置模型和套餐统计的请求格式。网关仍沿用 CLI 的 OAuth 应用，不使用桌面端的登录会话。

官网下载页前端脚本中还有门户会话接口 `https://codearts.huaweicloud.com/portal/snap-manager/v1/sso/user`，这是网页门户读当前登录用户用的，本网关不走浏览器 Cookie，因此不使用它。

## 原理

### 1. 公开 API 目录 vs 官方客户端网关

公开 API 目录只覆盖 collector-agents-access 上的用量同步。对话、模型列表和签到需要走官方客户端使用的 **snap-access 网关**，用 IAM/STS AK/SK 做 SDK-HMAC-SHA256 签名：

```
https://snap-access.cn-north-4.myhuaweicloud.com
```

该基址出现在官方 CLI 的生产配置中。下列路径在公开 API 目录中**没有**文档，可能随官方客户端变更：

| 用途 | 方法 | 路径 | 核实情况 |
| --- | --- | --- | --- |
| 动态模型列表 | `GET` | `/v1/model/builtin` | 官方 CLI 和桌面端 0.2.9 使用 `Agent-Type: PromptCenter`，响应模型数组为 `builtinModels` |
| 对话补全 | `POST` | `/api/v2/chat/completions` | 公开目录未收录；按 OpenAI Chat Completions 形态转发（未在本次 CLI 包中检索到完全相同的 `/api/v2/chat/completions` 字符串，调用前请用自己的账号验证） |
| 当前用户 | `GET` | `/snap-manager/v1/current/user` | 官方 CLI 登录后会签名调用 |
| 账号 / 套餐 / 积分额度 | `GET` | `/snap-manager/v1/statistics/plugin` | 官方 CLI 和桌面端 0.2.9 使用此路径；桌面端从 `metrics` 中读取积分总额、已用、剩余等数据 |
| 活动列表 / 领取 / 确认 | `GET /v1/ops/delivery?channel=DESKTOP`，`POST /v1/ops/claim`，`POST /v1/ops/confirm` | 已静态核对桌面端 0.2.9；使用 `Agent-Type: PromptCenter` 和 AK/SK 签名，临时凭据附带 `X-Security-Token`。CLI 凭据能否领取仍取决于上游授权 |
| 限时模型权益 | `POST` | `{opengw}/api/v1/benefit/claim` 空 JSON `{}` | 官方 CLI：生产基址 `https://opengw.developer.huaweicloud.com`，`error_code == "0000"` 为成功 |

用量同步类公开 API 不是 MVP 范围，本项目未实现。本项目不实现浏览器 Cookie 门户登录，也不读取或解密 IDE 本地凭据库。

### 2. 签名认证

实现依据官方签名文档，而不是任何第三方 SDK 源码拷贝：

1. 构造规范请求 CanonicalRequest（方法、规范化 URI、查询串、规范化头、SignedHeaders、请求体 SHA-256）。计算签名时 URI 必须以 `/` 结尾；实际发请求时可以不带这个尾斜杠。
2. 待签字符串：`SDK-HMAC-SHA256` + `X-Sdk-Date`（UTC `YYYYMMDDTHHMMSSZ`）+ CanonicalRequest 的 SHA-256。
3. `signature = Hex(HMAC-SHA256(SK, StringToSign))`。
4. `Authorization: SDK-HMAC-SHA256 Access={AK}, SignedHeaders=..., Signature=...`。

若存在临时安全令牌，加入**已签名**的 `X-Security-Token`。若账号配置了 `project_id`，加入已签名的 `X-Project-Id`。模型列表和签到请求按官方客户端的方式签名 `X-Language`（默认 `zh-cn`）；其他请求仅在配置 `settings.x_language` 时附加该头，不参与签名。

### 3. OAuth / SSO（官方 CLI 浏览器授权）

官方用户指南只写到「打开浏览器登录并确认授权」。具体授权 URL、换票和续期步骤来自官方 CLI 26.11.11 的实现，概述如下。

1. 生成 PKCE：`code_verifier` 为 64 字节的 hex（128 个字符），`code_challenge` 为不带填充的 base64url(SHA-256(verifier))，方法字面量为 `SHA-256`。另生成 `ticket_id`（32 字节 hex）。
2. 生成 ES256 / P-256 的 DPoP 密钥对。官方 CLI 在调用令牌接口时携带 `DPoP` 头（JWT typ 为 `dpop+jwt`，声明含 `htm`/`htu`/`iat`/`jti`）。
3. 在 `127.0.0.1` 上启动回调服务，路径为 `/oauth/callback`。官方 CLI 使用端口 `0`（由操作系统分配）。
4. 打开授权地址（官方 CLI 的 `client_id` 为 `CodeArts_Tui`）：

   `https://codearts.huaweicloud.com/portal/authorize?locale=zh-cn&client_id=CodeArts_Tui&port=<回调端口>&code_challenge=<S256>&code_challenge_method=SHA-256&ticket_id=<hex>`

5. 浏览器登录并确认授权后，门户会跳回本机回调：
   - 既无 `code` 也无 `secret`：视为探活，返回 200。
   - 带 `secret` 与 `redirect`：先 307 到 `redirect`，再轮询 `GET https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager/v1/login/ticket?ticket_id=&secret=`（官方 CLI 最多 10 次、间隔 1 秒）。票据体字段为 `credential.access` / `secret` / `securitytoken` / `expires_at`。
   - 带 `code`：`POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens`，表单为 `client_id`、`code`、`code_verifier`、`grant_type=authorization_code`、`redirect_uri=http://127.0.0.1:<port>/oauth/callback`，请求头带 `DPoP`。响应中的 `credentials`（`access_key_id` / `secret_access_key` / `security_token` / `expiration`）以及 `refresh_token` 为可用的临时凭证。
6. 临时凭证写入该账号的 `data/accounts/<id>.json`（Unix 权限 0600），**不要提交到仓库**。通过授权码取得刷新令牌时，过期前约 10 分钟会用同一把 DPoP 私钥和 `grant_type=refresh_token` 调用同一令牌地址续期（可带 `x-agent-user-account`）。若票据分支未提供可刷新的会话，失效后需要重新授权。

门户回调地址是环回地址。WebUI 支持自动接收回调；如果远程部署时浏览器无法访问该地址，也可以把授权后地址栏里的**完整回调地址**粘贴回 WebUI，无需转发回调端口。CLI 的 `uv run codearts2api login` 则仍需能访问回调端口，可在有浏览器的机器上执行，或使用 SSH 本地端口转发。`--print-only` 只负责打印授权链接，CLI 仍会等待本机回调。

`settings.credential_source`（默认 `auto`）用来筛选账号池类型：`auto` 使用全部启用账号，`oauth` 只用 OAuth 账号，`aksk` 只用 AK/SK 账号（旧值 `env` 仍被接受，并按 `aksk` 处理）。对话使用的具体账号由账号池策略决定（默认 `round_robin`，也可选 `first_available` 或 `random`）。

### 4. 动态模型列表

`GET /v1/models` 从上游 `GET /v1/model/builtin` 动态拉取，默认缓存 60 秒，`?refresh=true` 可强制刷新。只对外暴露 `enable=true` 的模型；若存在 `display_enabled` 字段，也必须为 true。对话请求使用裸模型 id（例如 `GLM-5.2`）；若客户端传来 `codearts/` 前缀，会去掉后再转给上游。

### 5. 每日签到

官方活动「每日签到得积分」面向已升级积分计费的个人版用户。本网关会先查套餐：`package.is_credit_package === true` 视为积分账号，否则返回 `ineligible`，不领取。

流程为：查询套餐 → 从 `DESKTOP` 活动列表匹配 `DAILY_CLAIM` → 带活动 `campaignId` 和 `idempotentKey` 领取 → 若返回领取记录 ID（`data.id` 或 `data.userBenefitId`），用 `userBenefitId` 确认。已有配置中的 `USER_LOGIN` 会按 `DAILY_CLAIM` 处理；桌面端的 `USER_LOGIN` 活动与每日签到不同。已领取返回 `already_claimed`，不视为错误。

签到复用账号已有的 CLI OAuth 临时 AK/SK 和安全令牌（或该账号配置的 AK/SK），不读取桌面端凭据。如果额度和模型可查询，但活动请求仍被上游拒绝，请通过官方桌面端或[门户](https://codearts.huaweicloud.com/portal)的「活动福利 → 每日领取」手动签到。若其他请求也提示会话过期，先在账号池「重新授权」。

若返回 `no_campaign`，不代表今日已领取。请用同一账号在官方桌面端或门户查看「每日领取」是可领取、已领取，还是未显示；积分计费的企业套餐不在本活动范围内。不要为排查勾选「同时领取其他可领取活动」，以免领取到其他活动。

定时签到使用进程内 APScheduler，时区默认 `Asia/Shanghai`，默认每天 08:00，启动时默认也会执行一次。如果活动已经标记为领取或确认，网关不会再次发送领取请求。

限时模型权益与每日签到分开：`POST /admin/benefit-claim` 按官方 CLI 调用 opengw 的 `/api/v1/benefit/claim`。

## 安装与启动

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。WebUI 的 HTML、CSS 和 JavaScript 位于 `src/codearts2api/static/`，随 Python 包安装；不需要 Node.js、npm 或单独生成 `WebUI` 目录。

```bash
cd codearts2oapi
uv sync
cp .env.example .env
# 编辑 .env，至少设置本地网关密钥 CA2A_API_KEY
```

准备上游凭证（任选，也可之后在 WebUI 里继续添加）：

```bash
# 方式 A：官方 CLI 同款浏览器登录（推荐），写入账号池
uv run codearts2api login
# 绑定到指定名称的账号（不存在则创建）
uv run codearts2api login --account 上班
# 无桌面环境、只想打印链接：
uv run codearts2api login --print-only

# 方式 B：直接启动网关，在 WebUI「账号池」中添加 AK/SK 或导入 Auth 文件
# config.json 和 accounts 目录会自动创建，不需要复制配置模板
```

启动网关：

```bash
uv run codearts2api
# 或
uv run codearts2api serve --host 127.0.0.1 --port 8787
```

默认监听 `127.0.0.1:8787`。浏览器打开控制台：

- http://127.0.0.1:8787/ （WebUI 默认入口）
- 旧链接 `/ui` 和 `/ui/` 会以 HTTP 307 重定向到 `/`。页面资源位于 `/static/`，管理 API 仍位于 `/admin/`，不会被页面路由覆盖。

如果反向代理把网关发布在 `/gateway/` 之类的子路径下，请让代理转发时去掉 `/gateway`，并为 ASGI 服务配置同一个 `root_path`（例如 `uv run uvicorn codearts2api.app:app --port 8787 --root-path /gateway`）。浏览器应访问 `/gateway/`（保留末尾斜杠）；静态资源、管理 API 和旧 `/ui` 入口都会保留此前缀。

页面本身不用带密钥；进入后在登录表单填写 `CA2A_API_KEY`，浏览器只把它存在 sessionStorage，并以 `Authorization: Bearer` 调用管理接口。在「账号池」里可以添加 AK/SK 或 OAuth 账号；OAuth 授权成功后账号才会保存。如果回调不能自动完成，可以在授权窗口粘贴浏览器地址栏里的完整回调地址。

其他命令：`uv run codearts2api status`（列出账号池，不输出密钥）、`uv run codearts2api logout`（可加 `--account`）。

```bash
uv run pytest
```

## Docker 与 GHCR

镜像由 GitHub Actions 在推送到 `main` 或手动触发工作流时构建，并发布到 GitHub Container Registry：

[`ghcr.io/leisurelyyrsc/codearts2oapi:latest`](https://github.com/LeiSureLyYrsc/codearts2oapi/pkgs/container/codearts2oapi)

`docker-compose.yml` 已指向该 `latest` 标签。先复制 `.env.example` 为 `.env`，**必须**设置 `CA2A_API_KEY`。启动时自动创建 `data/config.json`，只保存定时签到等运行配置；AK/SK 或 OAuth 认证保存在 `data/accounts/` 的每账号独立文件中。先启动，再在 WebUI 添加或导入账号即可。容器监听 `0.0.0.0:8787`，默认只映射到宿主机的 `127.0.0.1:8787`。控制台在 http://127.0.0.1:8787/ 。

```bash
cp .env.example .env
# 编辑 .env 后：
docker compose pull
docker compose up -d
```

Compose 默认以 `1000:1000` 运行网关，可在部署目录的 `.env` 中设置 `CA2A_UID` / `CA2A_GID`。它们只改变容器进程的身份，不会修改宿主机权限。若 Linux 上报 `PermissionError: ... '/app/data/accounts'`，请用拥有数据目录的普通用户运行 `id -u` 和 `id -g`，将结果填入 `.env`，并确保该用户能写入 `./data`、`./auths` 及其中现有文件。首次部署应先用该用户执行 `mkdir -p data auths`，再启动 Compose，避免 Docker 自动创建 root 所有的目录。

如果这两个目录已经由 root 创建，可在 Docker 宿主机上将它们移交给你配置的 UID/GID（下面以默认值为例；使用自定义值时替换 `1000:1000`）。无需清空目录，也不必把容器改成 root：

```bash
sudo chown -hR -- 1000:1000 ./data ./auths
docker compose pull
docker compose up -d --force-recreate
```

如果之前 `/ui` 或 `/` 无法打开，请先确认部署的是本仓库发布的 `ghcr.io/leisurelyyrsc/codearts2oapi:latest`，而不是旧的 `codearts2api` 镜像；更新 compose 后执行上面的 pull / up 命令重建容器。源码启动则更新代码后重启进程。若 `/` 返回 `WebUI is not packaged`，请重装完整的 Python 包或重新构建镜像；若 `/` 返回页面但按钮不可用或没有样式，请检查同一访问前缀下的 `static/app.js` 和 `static/styles.css` 是否都能返回相应文件（不应是 404 或 HTML）。Docker 构建会校验已安装的这三个 WebUI 文件。

要在修复发布前用当前源码验证镜像，可以本地构建，并让 Compose 不拉取远程标签：

```bash
docker build -t ghcr.io/leisurelyyrsc/codearts2oapi:latest .
docker compose up -d --pull never --force-recreate
```

首次拉取需要仓库里的 Actions 至少成功跑完一次。GHCR 包的可见性独立于仓库；若新发布的镜像无法匿名拉取，请由包管理员在 GitHub Packages 设置中将包设为 public。`./data`（运行配置与每账号独立 Auth 文件）会挂载到容器中以保持持久化；`./auths` 挂载仅保留用于旧 OAuth 会话的兼容迁移。Docker 部署不必额外映射 OAuth 回调端口：浏览器跳到本机地址后，把地址栏里的完整回调地址粘贴回发起授权的 WebUI 即可。

## 配置说明

首次启动或文件缺失时，项目自动生成包含默认设置的 `data/config.json`。`config.example.json` 只是运行配置参考，不需要复制，也不包含账号凭证。`.env` 只保留网关密钥启动默认值和进程级参数；账号 Auth 文件位于 `data/accounts/`。

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `CA2A_API_KEY` 或 `CODEARTS2API_API_KEY` | 保护 `/v1/*` 与 `/admin/*` 的启动默认值；也可在 WebUI 更换并写入 `data/config.json` | 空（未配置则受保护接口返回 503） |
| `CA2A_HOST` / `CA2A_PORT` | 本地监听（仅环境变量，不能在 WebUI 或配置文件中修改） | `127.0.0.1` / `8787` |
| `CA2A_DATA_DIR` | 账号池与运行配置目录（`config.json`，权限 0600） | `data` |
| `CA2A_AUTH_DIR` | 旧 OAuth 会话的迁移来源目录（新会话已在每账号 Auth 文件内） | `auths` |

为保持兼容，旧的 `CODEARTS_AK` / `CODEARTS_SK`（以及可选的 `CODEARTS_SECURITY_TOKEN`、`CODEARTS_PROJECT_ID`）在账号池为空时仍会导入为名为 `env` 的账号；若该账号还在，启动时会与当前环境变量同步。新部署请在 WebUI 添加或导入账号，密钥只写入对应 Auth 文件，不写入 config。首次生成 config 时会采用现有环境变量中的运行项（签到 cron、时区、超时等）；之后以 config / WebUI 为准。监听地址与数据目录仍然仅由环境变量控制。

### `data/config.json`

`settings` 中的常用项：

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `timezone` | 调度时区 | `Asia/Shanghai` |
| `checkin_cron` | 5 段 cron | `0 8 * * *` |
| `checkin_run_on_startup` | 启动时立即签到 | `true` |
| `scheduler_enabled` | 是否启用进程内定时签到 | `true` |
| `campaign_type` | 默认定向匹配的活动类型 | `DAILY_CLAIM` |
| `checkin_claim_all` | 是否同时领取其他可领取活动 | `false` |
| `model_cache_ttl` | 模型列表缓存秒数 | `60` |
| `snap_base_url` | snap-access 网关 | `https://snap-access.cn-north-4.myhuaweicloud.com` |
| `opengw_base_url` | 官方 CLI 中的权益网关 | `https://opengw.developer.huaweicloud.com` |
| `x_language` | 上游 `X-Language`；为空时模型列表和签到使用 `zh-cn` | 空 |
| `log_level` | 日志级别 | `INFO` |
| `http_timeout` / `http_stream_timeout` | 上游 HTTP 超时（秒）；流式读取可更长 | `60` / `300` |
| `credential_source` | `auto` / `oauth` / `aksk`（筛选账号池类型） | `auto` |
| `pool_strategy` | `round_robin` / `first_available` / `random` | `round_robin` |
| `cooldown_seconds` | 上游 401/403/429/5xx 后的账号冷却秒数 | `60` |
| `oauth_client_id` | 官方 CLI 为 `CodeArts_Tui` | `CodeArts_Tui` |
| `oauth_portal_url` | 官方门户 | `https://codearts.huaweicloud.com/portal` |
| `oauth_sts_url` | 官方 CLI 使用的 STS | `https://sts.cn-north-4.myhuaweicloud.com` |
| `oauth_locale` | 授权页语言 | `zh-cn` |
| `oauth_callback_port` | 本机回调端口，0 表示系统分配 | `0` |
| `api_key` | 可选，WebUI 保存后的网关密钥；未写入时使用环境变量 | 空 |

config 不再包含 `accounts`、AK/SK、`security_token` 或 `project_id`。这些字段只属于下面的每账号 Auth 文件。网关自身的 `settings.api_key` 仍可在 WebUI 保存（它不是上游账号凭证）。

日志为结构化 JSON，不会输出 AK/SK/令牌，并对日志中的密钥值脱敏。在 WebUI 中修改时区、cron 或调度开关时，进程内 APScheduler 会重新安排，无需重启。

## 多号池与 WebUI

1. 打开 http://127.0.0.1:8787/ ，输入 `CA2A_API_KEY`。
2. 在「账号池」中添加账号：AK/SK 直接填写密钥并保存；也可以点「连接 OAuth 账号」，填写名称后去华为云授权，授权成功才会添加。如果授权页跳转到 `127.0.0.1` 或 `localhost` 后显示「无法访问」，请从浏览器地址栏复制**完整地址（含 `?` 后的参数）**，粘贴回原来的授权窗口。回调含一次性凭证，请勿转发或分享。
3. 「签到与状态」可对全部或单个账号触发每日签到 / 权益领取，并查看调度器的下次运行时间。上次签到结果会写入对应的 `data/accounts/<id>.json`，重启后仍可在页面看到。
4. 管理接口：`GET/PUT /admin/config`，`GET/POST /admin/accounts`（直接添加仅支持 AK/SK），`PATCH/DELETE /admin/accounts/{id}`，`POST /admin/accounts/{id}/test`（探测套餐统计）。OAuth 用 `POST /admin/login/start`（`{name, enabled?, note?}`）发起新增，或 `POST /admin/accounts/{id}/login` 为已有账号重新授权。用返回的 `login_id` 查询 `GET /admin/login/status?login_id=…` 或通过 `DELETE /admin/login?login_id=…` 取消；手动回调则向 `POST /admin/login/callback` 提交 `{login_id, callback_url}`。普通管理响应中的 SK / security_token / refresh_token / 完整 AK 一律脱敏；显式的 Auth 导出接口是例外，会返回完整凭证。

在账号池中点击对应账号的「探测」可查询额度。`POST /admin/accounts/{id}/test` 使用该账号的凭据；响应中的 `quota` 包含上游返回的额度，缺失时不会按 0 显示。

## Auth 文件：每账号单文件、导入与导出

默认目录：`data/accounts/<id>.json`（随 `CA2A_DATA_DIR` 改变）。账号信息、认证与该账号签到状态在同一文件中；OAuth 的临时 AK/SK、刷新令牌、有效期和 DPoP 私钥也嵌入该文件，不再要求另外携带 session.json。文件原子替换，Unix 权限为 0600；Windows 的 `chmod` 不能替代 NTFS ACL，请把数据目录限制为当前服务账号可访问。

AK/SK Auth 文件的最小结构如下（请勿把真实密钥提交到仓库）：

```json
{
  "version": 1,
  "account": {
    "id": "primary",
    "name": "primary",
    "kind": "aksk",
    "enabled": true,
    "ak": "YOUR_ACCESS_KEY_ID",
    "sk": "YOUR_SECRET_ACCESS_KEY",
    "security_token": null,
    "project_id": null
  }
}
```

OAuth 文件使用 `"kind": "oauth"`，同一 JSON 的 `oauth_session` 字段保存完整会话。请使用项目导出的文件，避免手动编辑刷新令牌或 DPoP 数据。账号 ID 使用 1–128 位字母、数字、下划线或连字符；Windows 保留名称会映射为安全文件名。

- **WebUI**：账号行点击「导出 Auth」，确认敏感信息提示后下载单个 JSON；「导入 Auth 文件」支持一次选择多个 JSON。
- **API**（均需网关 Bearer 密钥）：`GET /admin/accounts/{id}/export` 下载完整 Auth（`Cache-Control: no-store`）；`POST /admin/accounts/import` 接受 `{"files": [<Auth JSON>, ...]}`。
- 导入最多 100 个文件、请求总量 4 MiB；校验版本、字段类型及必要凭证。ID、文件名或名称冲突（不区分大小写）会拒绝整批导入，不会静默覆盖；请先处理冲突后重试。导入成功响应只返回脱敏账号，不返回密钥。
- 无需重启即可通过 WebUI/API 导入。若手动把合法 Auth JSON 放到 accounts 目录，需重启读取，文件名须与账号 ID 对应。
- **导出文件含完整密钥、刷新令牌及私钥，请当作密码保管，勿上传到公开仓库或聊天。** 导出全部账号时可以复制整个 `data/accounts/` 目录；在线逐账号导出亦可。

### 旧部署自动迁移

停止旧进程并私下备份 `data/`、`auths/`，然后启动新版本即可，无需手工复制模板。启动时先把旧 `config.json` 中的 `accounts` 写成独立 Auth 文件，再把 config 改成仅含运行设置的 v2 格式；原 ID、名称、启用状态和签到信息保留。旧 `auths/session.json` 或被账号引用的会话也会嵌入对应 Auth 文件，成功落盘后移除已迁移的旧会话。

损坏文件、未知格式或冲突会明确报错并停止，不会覆盖损坏数据；迁移写入失败时会保留原 config 中的账号以便恢复。数据目录需要写权限。运行期间不要同时启动两个网关进程写同一份数据，也不要边运行边手动编辑 Auth 文件。旧环境变量引导产生的账号同样写到独立 Auth 文件中。

## 接口与调用示例

健康检查（无需密钥）：

```bash
curl -s http://127.0.0.1:8787/healthz
```

受保护接口需要 `Authorization: Bearer <CA2A_API_KEY>`。

```bash
# 发起 OAuth 授权；打开返回的 authorize_url，成功后才添加账号
curl -s http://127.0.0.1:8787/admin/login/start   -H "Authorization: Bearer $CA2A_API_KEY"   -H "Content-Type: application/json"   -d '{"name":"工作账号"}'
# 将 LOGIN_ID 替换为上一步返回的 login_id
curl -s "http://127.0.0.1:8787/admin/login/status?login_id=LOGIN_ID"   -H "Authorization: Bearer $CA2A_API_KEY"

curl -s http://127.0.0.1:8787/v1/models   -H "Authorization: Bearer $CA2A_API_KEY"
curl -s "http://127.0.0.1:8787/v1/models?refresh=true"   -H "Authorization: Bearer $CA2A_API_KEY"

curl -s http://127.0.0.1:8787/v1/chat/completions   -H "Authorization: Bearer $CA2A_API_KEY"   -H "Content-Type: application/json"   -d '{"model":"GLM-5.2","messages":[{"role":"user","content":"你好"}]}'

curl -sN http://127.0.0.1:8787/v1/chat/completions   -H "Authorization: Bearer $CA2A_API_KEY"   -H "Content-Type: application/json"   -d '{"model":"codearts/GLM-5.2","stream":true,"messages":[{"role":"user","content":"你好"}]}'

curl -s -X POST http://127.0.0.1:8787/admin/checkin   -H "Authorization: Bearer $CA2A_API_KEY"   -H "Content-Type: application/json"   -d '{"all_claimable": false}'

curl -s http://127.0.0.1:8787/admin/status   -H "Authorization: Bearer $CA2A_API_KEY"

curl -s http://127.0.0.1:8787/admin/accounts   -H "Authorization: Bearer $CA2A_API_KEY"
```

`GET /admin/checkin` 与 `GET /admin/status` 内容相同，并会附带各账号的签到结果。

## 限制与注意事项

- 对话、模型列表、签到、OAuth 换票所使用的部分路径**未写入公开 API 目录**。已在官方 CLI 或桌面端核对的会在上文标明；安装包里的请求格式不能证明实际账号具备权限。接口可能随官方版本调整。
- 必须使用**你自己的**华为云账号（浏览器登录或自行创建的 AK/SK）。不要分享密钥、`data/accounts/` 或导出的 Auth 文件。
- 对话会消耗账号积分。签到积分仅当天有效，数量以官方活动说明为准。
- 上游响应字段没有稳定的公开 schema。本网关在字段已接近 OpenAI 形态时做忠实转发，并对明显包装字段做适配，但不会臆造工具调用等行为。
- 未实现 IDE 本地凭据解密、插队或绕过限流。
- 自动化测试不调用真实华为云接口。接入前请用自己的账号联调登录、模型列表和对话。

## 许可证

MIT
