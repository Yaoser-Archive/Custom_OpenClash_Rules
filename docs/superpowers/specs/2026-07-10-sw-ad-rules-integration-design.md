# Shadowrocket AD/Proxy 规则集成设计

## 概述

将 [Shadowrocket-ADBlock-Rules-Forever](https://github.com/Johnshall/Shadowrocket-ADBlock-Rules-Forever) 的 `sr_cnip_ad.conf` 规则集成到本项目，创建新的 INI 订阅转换模板 `Custom_Clash_SW_AD.ini`。

## 目标

1. **广告拦截**：引入 ~60,000 条广告域名 REJECT 规则，替代已废弃的 Dnsmasq 广告过滤方案
2. **代理分流增强**：用 Shadowrocket 精细域名规则（200+ 条）替代部分 GeoIP 粗粒度分类，提升分流精准度
3. **自动化同步**：每日 GitHub Actions 自动下载、转换、发布，用户侧 rule-provider 自动更新

## 文件结构

### 新增文件（手动维护）

```
cfg/Custom_Clash_SW_AD.ini              ← 新 INI 订阅转换模板
.github/workflows/auto-sync-swrules.yml ← 每日同步 GitHub Actions 工作流
scripts/convert_sw_rules.py             ← Shadowrocket → OpenClash 转换脚本
```

### 自动生成文件（不手动编辑）

```
rule/SW_AD_Domain.yaml / .mrs           ← 广告域名 REJECT 规则
rule/SW_Proxy_Apple.yaml / .mrs         ← Apple 服务代理规则
rule/SW_Proxy_Disney.yaml / .mrs        ← Disney+ 代理规则
rule/SW_Proxy_Amazon.yaml / .mrs        ← Amazon/Prime Video 代理规则
rule/SW_Proxy_Telegram.yaml / .mrs      ← Telegram 代理规则
rule/SW_Proxy_AI.yaml / .mrs            ← AI 工具代理规则
rule/SW_Proxy_Microsoft.yaml / .mrs     ← Microsoft/GitHub 代理规则
rule/SW_Proxy_Other.yaml / .mrs         ← 其他代理规则（兜底）
```

## INI 设计

### 规则排序（从上到下优先命中）

1. **广告拦截**：`SW_AD_Domain.yaml` — REJECT（最高优先级）
2. **基础直连**：private、Custom_Direct、Steam CDN 等（不变）
3. **SW 精细代理**：按类别拆分的 SW_Proxy_*.yaml（替代部分 GeoIP）
4. **GeoIP 兜底**：保留 GEOSITE 规则覆盖 SW 未涉及的类别（YouTube、Netflix、社交媒体等）
5. **兜底直连**：`GEOSITE,cn` + `GEOIP,cn` + FINAL

### 代理分组

与现有 `Custom_Clash.ini` 保持一致的分组结构，包括：手动选择、自动选择、美国节点、全球直连，以及各应用分组（即时通讯、社交媒体、GitHub、ChatGPT、AI服务、YouTube、Netflix、DisneyPlus、HBO、PrimeVideo、AppleTV+、Emby、Spotify、Bahamut、国外媒体、国外电商、谷歌FCM、谷歌服务、苹果服务、微软服务、游戏平台、Steam、测速工具、漏网之鱼、非标端口）。

## 转换脚本设计

### 输入

- URL: `https://johnshall.github.io/Shadowrocket-ADBlock-Rules-Forever/sr_cnip_ad.conf`

### 处理流程

1. 下载原始 Shadowrocket 规则文件
2. 按 action 分类：
   - `DOMAIN-SUFFIX,*,Reject` + `IP-CIDR,*,Reject` → SW_AD_Domain.yaml
   - `DOMAIN-SUFFIX,*,Proxy` + `IP-CIDR,*,Proxy` → 按域名模式归类到各 SW_Proxy_*.yaml
3. 过滤过于宽泛的规则（`DOMAIN-KEYWORD,amazon`、`DOMAIN-KEYWORD,aws`）
4. 去重排序
5. 输出 OpenClash YAML 格式（`+.domain` 前缀）
6. 调用 mihomo 生成 MRS 二进制

### 分类规则映射

| 源域名模式 | 目标文件 |
|-----------|---------|
| `*.apple.*`, `*.icloud.*`, `*.itunes.*` | SW_Proxy_Apple.yaml |
| `*.disney.*`, `*.bamgrid.*`, `*.dssott.*` | SW_Proxy_Disney.yaml |
| `*.amazon.*`, `*.primevideo.*`, `*.audible.*` | SW_Proxy_Amazon.yaml |
| `*.telegram.*`, `t.me` | SW_Proxy_Telegram.yaml |
| `copilot.*`, `devv.ai`, `forefront.ai`, `github.dev` | SW_Proxy_AI.yaml |
| `*.microsoft.*`, `*.office.*`, `*.live.*`, `raw.githubusercontent.com` | SW_Proxy_Microsoft.yaml |
| 其余 Proxy 规则 | SW_Proxy_Other.yaml |

## GitHub Actions 工作流

- **触发方式**：`schedule`（每日 UTC 0:00）+ `workflow_dispatch`（手动）
- **步骤**：
  1. Checkout 仓库
  2. 设置 Python 3.x
  3. 运行 `scripts/convert_sw_rules.py` 下载并转换
  4. 安装 mihomo，将 YAML 转为 MRS
  5. 如有变更，commit & push

## 用户使用方式

1. 在 OpenClash 订阅转换中，选择 `Custom_Clash_SW_AD.ini` 作为模板
2. 订阅链接使用规则提供者 URL（jsdelivr CDN）
3. 规则每日自动更新，无需手动干预

## 技术约束

- OpenClash `Fake-IP` 模式（与项目现有方案一致）
- MRS 二进制格式用于减小加载体积
- 广告 REJECT 行为通过 rule-provider 的 `behavior` 字段控制
- DOMAIN-KEYWORD 类宽泛规则自动过滤，避免误代理
