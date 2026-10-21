# V2 本地隔离演示与并发检查

本说明只用于学习、求职演示，不是生产部署。AI 问题、评分、报告和简历分析均为明确的确定性替身，邮件写入私有运行目录的 outbox；PDF 文本提取、认证、PostgreSQL、Redis、迁移与 HTTP/SSE 路由实际运行。正式 `main.py` 不导入替身。API/DB/Redis 仅在 `internal: true` 网络中；一个固定 Nginx 反向代理连接入口与内部网络，将接口发布到本机回环地址。真实外部平台调用不可用。

## 正式发布后的常规克隆流程

V2 正式发布后，从 [GitHub 仓库](https://github.com/d18399709571/ai-mock-interview-system) 克隆包含 V2 修复的最新版本，即可取得完整源码和演示文件。普通使用者不需要审核包、`changes.patch` 或作者电脑上的验收目录。本节描述发布后的使用流程，不表示 V2 已经推送；发布前状态与旧基线仅作为文末的历史记录保留。

```powershell
git clone https://github.com/d18399709571/ai-mock-interview-system.git ai-mock-interview-system
$Repo = (Resolve-Path -LiteralPath '.\ai-mock-interview-system').Path
```

克隆后先确认该版本包含 `docs/demo/prepare.py`、`compose.yml`、`demo_app.py`、`smoke.py`、`nginx.conf`、`Dockerfile` 及其忽略文件，并核对发布记录。若仍是文末的旧 SHA 或缺少这些文件，说明取得的版本尚不含完整 V2，应停止，不要执行旧部署脚本替代。

## 已验证的环境与运行条件

已验证的是 **Windows 宿主机 + PowerShell + Docker Desktop Linux 引擎（`desktop-linux` context）**，不是 Windows 容器。后端在 Linux 容器中使用 Python 3.12；宿主机需有 Git、Python 3、Node/npm 和可用的 Docker Compose。前端按仓库中的 npm lockfile 安装。未验证 macOS、原生 Linux、其他 Docker context 或无 F 盘环境的完整操作流程。

`prepare.py` 当前明确要求：运行目录位于 **F 盘、此前不存在、且在克隆仓库之外**；它不会复用已有目录。项目名必须匹配 `interview-v2-demo-[a-z0-9-]+`。下面的目录与项目名是可替换示例，必须由使用者选择新的值；不能直接复用历史验收目录。端口默认是 `127.0.0.1:18096`。执行前确认项目名、镜像 Tag、卷、网络与端口没有碰撞，碰撞时停止。

验收时复用了本机已有的公开镜像 `postgres:16.14-alpine3.23`、`redis:8.8.0-alpine` 和 `nginx:1.30.6-alpine3.24`；Nginx 配置为 `pull_policy: never`。使用者也需先具备这些镜像，缺失时应停止并另行准备公开依赖，不能依赖作者的本地镜像或覆盖其他项目的 Tag。API 镜像通过下列已验证的 `build api` 命令从仓库 Dockerfile 和公开 Python 依赖构建，不依赖旧验收专用镜像；首次构建需要可访问公开依赖源。仓库对全部 Python 传递依赖尚未锁定，未来解析可能变化。

以下服务命令、脚本路径与参数沿用已验证流程，仅将作者的固定路径换成使用者的克隆路径与新运行目录；替换后的示例路径本身未另行启动验收。禁止覆盖旧资源、读取旧私人配置或直接执行后端历史 Compose/deploy 脚本。若另行接入真实服务，密钥应由用户在仓库外提供，禁止提交 `.env`；真实服务接入不在本指南的验收范围内。

```powershell
$Runtime = 'F:\InterviewDemoRuntime\v2-demo-01'
$Compose = Join-Path $Repo 'docs\demo\compose.yml'
$EnvFile = Join-Path $Runtime 'demo.env'
$Project = 'interview-v2-demo-local01'
python -B "$Repo\docs\demo\prepare.py" --runtime $Runtime --project $Project
docker --context desktop-linux compose --env-file $EnvFile -f $Compose config --quiet
docker --context desktop-linux compose --env-file $EnvFile -f $Compose build api
docker --context desktop-linux compose --env-file $EnvFile -f $Compose up -d postgres redis
docker --context desktop-linux compose --env-file $EnvFile -f $Compose run --no-deps --rm -w /runtime/backend api alembic upgrade head
docker --context desktop-linux compose --env-file $EnvFile -f $Compose up -d --wait --wait-timeout 60 proxy
docker --context desktop-linux compose --env-file $EnvFile -f $Compose exec -T api python -B /source/docs/demo/smoke.py
```

逐条检查退出码，失败时停止。新库迁移只运行 `upgrade head`，不清空数据库或删除记录。`prepare.py` 不启动服务、不安装软件；在新的私有目录生成随机合成配置，并创建哈希一致的后端运行副本。现有日志模块按源码路径写日志，故日志必须落入该私有副本；克隆仓库始终只读挂载。API/DB/Redis 仅连接内部网络，入口代理同时连接入口网络和内部网络。不要将外部 `demo.env`、outbox、账号文件或数据卷放入 Git。

`smoke.py` 通过真实 TCP HTTP 执行健康检查、合成账号注册/Redis 验证/登录、合成 PDF 上传与文本解析、三题与五题面试/报告、管理员登录与读取。验证代码与 Token 不写日志。邮箱在 `$Runtime\demo-accounts.json`；演示账号密码仅在该目录的私有 `demo.env` 中。不要使用真实身份或密钥。

## 前端

将前端源码复制到新建的私有运行目录，安装、构建与缓存全部留在该目录。两端构建已经验证；这不等同于已经通过人工浏览器全流程。

```powershell
Copy-Item -LiteralPath "$Repo\ai-interview-frontend" -Destination "$Runtime\client" -Recurse
Copy-Item -LiteralPath "$Repo\ai-interview-admin" -Destination "$Runtime\admin" -Recurse
npm.cmd --prefix "$Runtime\client" ci --ignore-scripts --no-audit --no-fund --cache "$Runtime\npm-cache"
npm.cmd --prefix "$Runtime\admin" ci --ignore-scripts --no-audit --no-fund --cache "$Runtime\npm-cache"
npm.cmd --prefix "$Runtime\client" run build
npm.cmd --prefix "$Runtime\admin" run build
```

Vite 配置支持通过 `BACKEND_PROXY_TARGET` 指定开发代理目标。人工启动前端并完成浏览器交互的流程未纳入 V2 专项验收，因此这里不补充未经该专项验证的启动命令，也不将 README 中的历史截图视为 V2 新证据。

## 答题契约与恢复

两个提交路径都要求：`{"answer":"合成回答","expected_question_index":0}`，题号从 0 开始。题号必须是非负整数；旧客户端缺少该字段或传字符串/布尔值会得到请求验证错误（当前统一响应为 HTTP 400）。没有无保护兼容入口。

服务先按用户归属获取 `Interview FOR UPDATE NOWAIT`，再校验状态与预期题号。冲突的普通 HTTP 返回 409；SSE 流已打开时返回 `type:error, code:answer_conflict, retry_safe:false`，不能再改变 HTTP 状态码。只有评分/消息/索引/终报事务提交成功后才发送 `done`。

断流后先核对服务器消息与报告。结果未确定时不会自动将答案提交到下一题；“重试原题”保留原题号。另一标签页已提交本题时，核对会恢复服务器上的实际答案。超时、异常、取消会回滚未提交事务；提交确认丢失时以服务器记录为准。

当前最小方案在 AI 与 SSE 期间持有一条数据库连接及一个面试行锁，单次保护超时为 90 秒，同题冲突不会排队；隔离 PostgreSQL 还设置 120 秒空闲事务超时。连接池为现有 20+10。只适合小规模演示，不能据此宣称高并发或生产可用。若需吞吐扩展，另行设计有租期、可恢复的持久占用记录。

## 定向验证与停止

```powershell
docker --context desktop-linux compose --env-file $EnvFile -f $Compose exec -T api python -B /source/ai-interview-backend/tests/test_pg_answer_concurrency.py
docker --context desktop-linux compose --env-file $EnvFile -f $Compose ps
docker --context desktop-linux compose --env-file $EnvFile -f $Compose stop
```

并发脚本使用独立真实 PostgreSQL 会话、同步屏障、请求/提交时间、PID、数据库快照和强制断言。固定 AI 仅用于控制重叠，不是 FakeDB。脚本会创建合成数据，不清库、不删历史记录。保留失败日志并在修正后复验。停止命令只停止本轮项目；不得使用 `down -v`、prune 或删除任何卷。

历史生产配置及真实 DeepSeek/OpenAI/SMTP/AWS 的可用性不在本次演示验收范围内。

## 历史验收与补丁复现记录（普通运行不需要）

2026-10-21 的 V2 验收冻结基线为 `99a72740175ec054210529f63ad87e77dcbe732d`，Tree 为 `43ce0fbcb53f308320e647f9bea21acb27f7bb97`，原版 179 个文件。该 SHA 不含 V2 修复及本指南。验收时分别从 GitHub 克隆基线，在独立验证克隆应用 `changes.patch` 并按文件清单复制 `new-files/`，重建 188 个文件的 V2 工作树。此方法只用于持有对应审核包的历史审计，不是正式发布后的常规安装步骤。

历史工作区为 `F:\CodexRuntimes\interview-v2-closeout-99a7274`；验证源码在 `verification-clone`，私有运行数据在 `isolated-runtime\clean-demo`，项目名为 `interview-v2-demo-99a7274`。这些是作者机器上的证据位置，普通使用者不需要也不应依赖它们。上述新路径示例沿用相同脚本约束与服务命令，没有声称已在另一台机器或其他操作系统验证。

在本次仅限文档的收尾核对中，GitHub `main` 仍为上述冻结 SHA，尚未执行 V2 Commit/Push；这是截至 2026-10-21 该次核对的状态，不是对未来发布状态的断言。并发修复及隔离演示已验证；Docker 引擎恢复前后内置 `bridge` 网络 ID 变化的保护审计例外继续保留为 **PARTIAL**，未恢复旧网络，也不改写为所有环境完全未变。
