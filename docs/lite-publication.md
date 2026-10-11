# Lite 模板与规则发布

终端继续导入或刷新原订阅。维护者通过仓库更新模板和规则，192 按发布清单获取已验证内容；客户端无需添加新设置。

## 发布内容

Lite 保留原 14 个分组名称、默认选择和检测间隔。192 在私有基线上保留 Novix、固定 CF、Reality 三条独立线路，默认 Novix。所有 Google 服务（包括 google-cn、字体、下载和 FCM）优先于直连规则走代理；两个 Google 分组禁止可达的直连选项。`sc`、`nj`、`cyber`、`cybercd2`、`dragon`、`dsh` 的 `.oribit.cn` 域名采用精确 DOMAIN 直连，随后 DOMAIN-SUFFIX 将父域及其余子域交给手动选择。34 条规则的顺序由契约检查固定，例外不扩大到更深子域。游戏下载采用上游的域名和 IP 两份 CDN 规则，Google Play 等 Google 下载仍走代理。国内直连域名规则每 1800 秒更新，其余每 28800 秒更新。

`main` 是源文件入口；`published` 是通过检查的 Lite 入口，包含模板、8 份规则及 `manifest.json`。其中旧 Steam 规则为兼容旧模板保留，新 Lite 使用 7 个规则提供者。清单固定仓库、源提交、上游提交和逐文件 SHA256。192 从清单中的源提交下载，避免一次更新混入不同版本。

## 工作流

`Validate Lite template` 对 PR、相关 main 变更和手动运行执行 `validate-lite` 校验。使用占位节点，检查分组、引用、完整规则顺序，再用固定转换器和 Mihomo 加载转换结果。它不接收真实节点或订阅认证信息。

`Auto sync rules from Aethersailor` 每两小时、相关 main 变更或手动触发时执行：

1. 锁定一个上游提交，下载全部规则到临时目录。
2. 检查 HTTP 状态、超时、大小、YAML、规则类型、模板和转换后语义；任一失败不写入分支。
3. 同步 main 后发布同一已验证快照至 published。普通快进推送拒绝并发覆盖；分支前进时最多重新构建、验证三次。认证或网络错误直接报错。
4. 在同一运行中刷新缓存并核对实际客户端 URL 的字节哈希。流程不会依赖机器人推送触发下一次工作流；这是 [GitHub 的事件触发限制](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)。

发布和手动缓存刷新共用串行队列。先检查客户端 URL 的字节哈希，仅陈旧内容申请 purge；取得任务 ID 后查询该任务状态，不因 pending 重复提交。最多 3 个并发，每个 URL 限时 120 秒，单次请求最多 10 秒，并尊重 Retry-After。刷新完成仍必须检查实际字节。逐文件 JSON 日志与摘要保留阶段、HTTP 状态、任务结果和哈希。缓存失败使运行失败，分别记录“规则已发布”和“缓存失败”，不会撤销已验证发布。重跑缓存工作流时，已经一致的文件不再 purge。

192 每 15 分钟检查最新发布清单。固定源提交下载模板和规则，校验并转换两格式候选，保留私有节点、DNS、认证、计量和 Novix 默认选择。正式订阅的规则 URL 固定到同一提交，缓存路径包含文件哈希；通过核心、私有字段及主 Novix 通路检查后原子切换。失败继续服务上一版；本地未审核的新模板结构会被拒绝，不下载执行远端 Python。网关每 15 分钟只刷新 convert_oribit；其他客户端使用自身的订阅刷新设置。

清理工作流只分页统计失败和取消记录，不删除日志。Dependabot 自动合并只接受本仓库机器人 PR，要求 PR 仍指向本次成功运行的提交，且同一检查套件中的 `validate-lite` 成功；不检出或执行 PR 代码。

## 验证与工具

工具版本和 SHA256／镜像摘要在 `.github/pinned-tools.json`；Python 依赖在 `.github/requirements.txt`。转换器使用已验证的 extended v1.9.13 镜像摘要，Mihomo v1.19.31，actionlint v1.7.12。

在临时 Linux 环境运行（需要 Docker）：

```bash
python3 -m pip install -r .github/requirements.txt
python3 -B -m unittest discover -s .github/scripts -p 'test_*.py'
TOOLS=$(mktemp -d)
python3 .github/scripts/setup_tools.py "$PWD" "$TOOLS"
cp -r cfg rule "$TOOLS/candidate/"
"$TOOLS/actionlint" .github/workflows/*.yml
python3 .github/scripts/validate.py --root "$TOOLS/candidate" \
  --config-path /base/config/lite-ci/cfg/Custom_Clash_Lite.ini --core "$TOOLS/mihomo"
docker rm -f lite-validation
```

分支发布程序仅允许在 Actions 的临时检出中运行，避免重置维护者本地工作。故障用例覆盖 404、HTML、空／损坏 YAML、缺失资源、错误 IP 语义、分组默认值漂移、缓存过期、并发推送和发布文件白名单。

自动模板更新只导入已验证的分组、规则和规则提供者，DNS 逐值沿用私有基线；不会隐式部署历史 Google DoH 草稿。靠前的 Google 路由及 DNS IP 代理规则禁止 Google 分流到直连。启用开关绑定固定仓库、本地契约及更新程序哈希，无需手工三客户端验收文件。健康检查区分未启用、失败、版本落后和超过一小时未检查；备份包括更新单元、开关及完整快照元数据。

核心加载、实际路由和网站可达性分别报告。Stash 格式的核心重放不能视为原生手机验收；手机问题按实际反馈处理。保留显式 Google DNS 工具的独立测试，但本次不声明 DNS 已改为国外解析。
