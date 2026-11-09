# CustomRule Lite

面向 OpenClash / Mihomo 的 Lite 订阅转换模板及分流规则。

## 使用

将下面的模板地址填入订阅转换服务，生成并定期刷新完整订阅。

```text
https://raw.githubusercontent.com/Yaoser-Archive/Custom_OpenClash_Rules/published/cfg/Custom_Clash_Lite.ini
```

Lite 提供 14 个策略组和 34 条有序规则。Google 服务走代理，六个指定 oribit.cn 子域精确直连，游戏下载使用独立 CDN 规则。规则提供者通过 JSDMirror 固定到同一个完整提交 SHA。

标准版、Full 和 GFW 模板已退役，历史内容可从 Git 记录获取。

## 维护

- PR 和手动运行执行 Lite 校验。
- 每两小时、相关 main 变更及手动运行执行规则同步、验证和原子发布。
- Dependabot 自动合并要求对应 PR 修订的 Lite 校验成功。

发布内容包括 Lite、八份规则和 manifest v1。Lite 使用七个提供者，Steam 规则作为旧版本兼容附件。

发布步骤与镜像校验分别报告结果。镜像下载或哈希校验失败时保留失败诊断。详细步骤见 [发布与恢复说明](docs/lite-publication.md)。

## 来源与许可

规则来源为 [Aethersailor/Custom_OpenClash_Rules](https://github.com/Aethersailor/Custom_OpenClash_Rules)，转换器为 [SubConverter-Extended](https://github.com/Aethersailor/SubConverter-Extended)，核心为 [Mihomo](https://github.com/MetaCubeX/mihomo)。感谢上游项目及其贡献者。

本项目沿用 [CC-BY-SA-4.0](LICENCE) 许可。
