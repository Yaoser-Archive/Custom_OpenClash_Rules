# Lite 模板与规则发布

终端继续导入或刷新原订阅。维护者通过仓库更新模板和规则，192 按发布清单获取已验证内容；客户端无需添加新设置。

## 发布内容

Lite 保留原 14 个分组名称、默认选择和检测间隔。192 输出仍有固定 CF、Reality 两条线路，默认固定 CF，Reality 手动选择。按用户要求，所有 Google 服务（包括 google-cn、字体、下载和 FCM）优先于直连规则走国外代理；两个 Google 分组移除直连选项。游戏下载采用上游的域名和 IP 两份 CDN 规则，Google Play 等 Google 下载仍走代理。国内直连域名规则每 1800 秒更新，其余每 28800 秒更新。规则保持 YAML；局域网直连、IP 的 `no-resolve` 和最终兜底顺序均由契约检查固定。

`main` 是源文件入口；`published` 是通过检查的 Lite 入口，包含模板、8 份规则及 `manifest.json`。其中旧 Steam 规则为兼容旧模板保留，新 Lite 使用 7 个规则提供者。清单固定仓库、源提交、上游提交和逐文件 SHA256。192 从清单中的源提交下载，避免一次更新混入不同版本。

## 工作流

`Validate Lite template` 对 PR、相关 main 变更和手动运行执行 `validate-lite` 校验。使用占位节点，检查分组、引用、完整规则顺序，再用固定转换器和 Mihomo 加载转换结果。它不接收真实节点或订阅认证信息。

`Auto sync rules from Aethersailor` 每两小时、相关 main 变更或手动触发时执行：

1. 锁定一个上游提交，下载全部规则到临时目录。
2. 检查 HTTP 状态、超时、大小、YAML、规则类型、模板和转换后语义；任一失败不写入分支。
3. 同步 main 后发布同一已验证快照至 published。普通快进推送拒绝并发覆盖；分支前进时最多重新构建、验证三次。认证或网络错误直接报错。
4. 在同一运行中刷新缓存并核对实际客户端 URL 的字节哈希。流程不会依赖机器人推送触发下一次工作流；这是 [GitHub 的事件触发限制](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)。

发布和手动缓存刷新共用串行队列。缓存失败使运行失败，摘要分别记录“规则已发布”和“缓存失败”，不会把 published 分支恢复为未验证内容。192 首次接入新模板前也检查规则 URL 哈希，缓存不一致时保留旧订阅。可单独重跑手动缓存工作流。

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

192 对 Google 增加独立 DNS 策略：仅用国外 Google DoH；Mihomo 显式绑定谷歌服务分组，Stash 使用 follow-rule 配合靠前的 DNS IP 代理规则。依据 [Mihomo DNS 文档](https://wiki.metacubex.one/config/dns/) 与 [Stash DNS 文档](https://stash.wiki/features/dns-server) 分别生成，保留真实传输字段和其他业务的 DNS 配置。发布契约禁止同步把 Google 改回直连，不能使用国内 DNS 作为 Google 的回退。

核心加载、隔离 Google DNS 正常／故障测试与真实终端 DNS 防泄露是不同的验收项。每次首次接入仍须核查 Mihomo、Stash 和 OpenClash 的生效配置、国内直连、Google 及其他代理域名解析路径以及代理故障后的行为。服务器格式重放不能代替手机或路由器验收。
