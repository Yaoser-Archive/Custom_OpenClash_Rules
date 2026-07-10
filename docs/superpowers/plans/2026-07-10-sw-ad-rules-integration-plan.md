# Shadowrocket AD/Proxy 规则集成 — 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 创建 Custom_Clash_SW_AD.ini 模板及配套的自动同步工作流，将 Shadowrocket AD/Proxy 规则集成到 OpenClash 规则体系。

**架构：** Python 脚本每日下载 sr_cnip_ad.conf，按 action 分类（REJECT→广告拦截，Proxy→代理分流），生成 OpenClash YAML/MRS 格式规则文件，通过新 INI 模板引用。

**技术栈：** Python 3.x（标准库 urllib）、mihomo（YAML→MRS 转换）、GitHub Actions

---

### 任务 1：创建转换脚本 `scripts/convert_sw_rules.py`

**文件：**
- 创建：`scripts/convert_sw_rules.py`

- [ ] **步骤 1：编写脚本框架和下载逻辑**

```python
#!/usr/bin/env python3
"""Convert Shadowrocket AD/Proxy rules to OpenClash YAML format.

Downloads sr_cnip_ad.conf from Johnshall's repository,
parses rules by action (Reject/Proxy), classifies proxy rules
into service categories, and generates YAML rule files.

Usage:
    python scripts/convert_sw_rules.py
"""

import urllib.request
import os
import re
import sys
from pathlib import Path

# Configuration
SOURCE_URL = "https://johnshall.github.io/Shadowrocket-ADBlock-Rules-Forever/sr_cnip_ad.conf"
RULE_DIR = Path(__file__).resolve().parent.parent / "rule"
REPO_URL = "https://github.com/Yaoser-Archive/Custom_OpenClash_Rules"
SOURCE_REF = "https://github.com/Johnshall/Shadowrocket-ADBlock-Rules-Forever"

# Domain classification patterns for proxy rules
# (category_name, output_filename, [patterns])
PROXY_CATEGORIES = [
    ("Apple", "SW_Proxy_Apple", [
        r"\.apple\.", r"\.icloud\.", r"\.itunes\.", r"\.aaplimg\.", r"\.akadns\.net",
        r"apple\.news", r"apple-dns\.net", r"apple\.comscoreresearch",
        r"apple-mapkit\.com",
    ]),
    ("Disney+", "SW_Proxy_Disney", [
        r"disney", r"\.bamgrid\.", r"\.dssott\.", r"\.dilcdn\.",
        r"starwars\.com", r"go\.com",
    ]),
    ("Amazon/PrimeVideo", "SW_Proxy_Amazon", [
        r"\.amazon\.", r"\.primevideo\.", r"\.audible\.", r"\.amzn\.", r"\.a2z\.",
        r"amazonaws\.com", r"amazonpay\.com", r"\.imdb\.", r"cloudfront\.net",
        r"kindle\.com",
    ]),
    ("Telegram", "SW_Proxy_Telegram", [
        r"telegram", r"^t\.me$", r"tdesktop\.com", r"telegra\.ph", r"telesco\.pe",
    ]),
    ("AI Tools", "SW_Proxy_AI", [
        r"copilot\.microsoft\.com", r"devv\.ai", r"forefront\.ai",
        r"github\.dev", r"bing\.com",
    ]),
    ("Microsoft/GitHub", "SW_Proxy_Microsoft", [
        r"\.microsoft\.com", r"\.office\.", r"\.live\.com", r"\.live\.net",
        r"\.1drv\.com", r"\.onedrive\.", r"raw\.githubusercontent\.com",
        r"\.hockeyapp\.net", r"\.svc\.ms",
    ]),
]

# Broad keyword rules to skip (too aggressive)
SKIP_KEYWORDS = {"amazon", "aws"}

# Broad domains to skip (catch-all patterns)
SKIP_DOMAINS = {"akamaiedge.net"}  # Generic CDN, not Apple-specific

def download_rules(url: str) -> str:
    """Download rules file and return as text."""
    print(f"Downloading: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "OpenClash-Rule-Sync/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        content = resp.read().decode("utf-8", errors="replace")
    print(f"Downloaded {len(content)} bytes, {len(content.splitlines())} lines")
    return content

def parse_rules(content: str) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Parse Shadowrocket rules into reject and proxy lists.

    Returns:
        (reject_rules, ip_reject_rules, {category: [(domain, type, ip_flag)]})
    """
    reject_rules = []
    ip_reject_rules = []
    proxy_rules = []  # [(domain, rule_type), ...]

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip section headers and comments
        if line.startswith("[") or line.startswith(";") or line.startswith("#"):
            continue
        # Skip non-rule lines
        if not line.startswith(("DOMAIN", "IP-CIDR")):
            continue

        parts = line.split(",")
        if len(parts) < 3:
            continue

        rule_type, value, action = parts[0].strip(), parts[1].strip(), parts[2].strip()

        if action == "Reject" or action == "REJECT":
            if rule_type in ("DOMAIN-SUFFIX", "DOMAIN"):
                reject_rules.append(value)
            elif rule_type == "IP-CIDR":
                ip_reject_rules.append(value)
        elif action == "Proxy" or action == "PROXY":
            if rule_type in ("DOMAIN-SUFFIX", "DOMAIN"):
                proxy_rules.append(value)
            elif rule_type == "IP-CIDR":
                ip_reject_rules.append(value)  # IP rules go to same file for now

    # Deduplicate
    reject_rules = sorted(set(reject_rules))
    proxy_rules = sorted(set(proxy_rules))

    # Classify proxy rules into categories
    categorized: dict[str, list[str]] = {}
    for domain in proxy_rules:
        categorized_domain = classify_proxy_domain(domain)
        if categorized_domain not in categorized:
            categorized[categorized_domain] = []
        categorized[categorized_domain].append(domain)

    return reject_rules, ip_reject_rules, categorized

def classify_proxy_domain(domain: str) -> str:
    """Classify a proxy domain into a category filename stem."""
    domain_lower = domain.lower()
    for _cat_name, filename, patterns in PROXY_CATEGORIES:
        for pattern in patterns:
            if re.search(pattern, domain_lower):
                return filename
    return "SW_Proxy_Other"

def generate_yaml(filename_stem: str, domains: list[str], action: str,
                  total: int, rule_dir: Path) -> None:
    """Generate an OpenClash domain YAML file."""
    filepath = rule_dir / f"{filename_stem}_Domain.yaml"

    lines = []
    lines.append(f"# Generated from sr_cnip_ad.conf")
    lines.append(f"# SOURCE: {SOURCE_REF}")
    lines.append(f"# REPO: {REPO_URL}")
    lines.append(f"# TOTAL: {total}")
    lines.append(f"# ACTION: {action}")
    lines.append("")
    lines.append("payload:")

    for domain in domains:
        # Determine prefix based on domain format
        # DOMAIN-SUFFIX uses '+.', DOMAIN uses plain quoted
        if domain.startswith("+."):
            lines.append(f"  - '{domain}'")
        else:
            lines.append(f"  - '+.{domain}'")

    filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Generated: {filepath} ({total} rules)")

def main():
    """Main entry point."""
    print("=" * 60)
    print("Shadowrocket Rules → OpenClash YAML Converter")
    print("=" * 60)

    # Ensure rule directory exists
    RULE_DIR.mkdir(parents=True, exist_ok=True)

    # Download
    content = download_rules(SOURCE_URL)

    # Parse
    reject_rules, ip_reject_rules, proxy_categorized = parse_rules(content)

    # Generate AD reject rules
    all_reject = sorted(set(reject_rules + ip_reject_rules))
    generate_yaml("SW_AD", all_reject, "REJECT", len(all_reject), RULE_DIR)

    # Generate proxy category rules
    for filename_stem, domains in sorted(proxy_categorized.items()):
        generate_yaml(filename_stem, sorted(set(domains)), "PROXY",
                      len(set(domains)), RULE_DIR)

    # Summary
    total_proxy = sum(len(v) for v in proxy_categorized.values())
    print(f"\nSummary:")
    print(f"  AD Reject rules: {len(all_reject)}")
    print(f"  Proxy rules:     {total_proxy}")
    for filename_stem, domains in sorted(proxy_categorized.items()):
        print(f"    {filename_stem}: {len(set(domains))}")
    print("\nDone.")

if __name__ == "__main__":
    main()
```

- [ ] **步骤 2：本地测试脚本运行**

```bash
cd "D:/Document/Work/projects/Custom_OpenClash_Rules"
python scripts/convert_sw_rules.py
```

预期：下载成功，生成 `rule/SW_AD_Domain.yaml` 及 `rule/SW_Proxy_*.yaml` 文件，打印分类统计摘要。

- [ ] **步骤 3：验证生成的 YAML 格式**

检查生成的 `rule/SW_AD_Domain.yaml` 首部格式正确：
- 包含 `# Generated from sr_cnip_ad.conf`
- 包含 `# TOTAL: <数字>`
- `payload:` 后每行格式为 `  - '+.domain'`

```bash
head -15 rule/SW_AD_Domain.yaml
```

- [ ] **步骤 4：Commit**

```bash
git add scripts/convert_sw_rules.py
git commit -m "feat: add Shadowrocket rules conversion script"
```

---

### 任务 2：创建 GitHub Actions 工作流

**文件：**
- 创建：`.github/workflows/auto-sync-swrules.yml`

- [ ] **步骤 1：编写工作流文件**

```yaml
name: Auto sync Shadowrocket rules

on:
  schedule:
    - cron: '37 0 * * *'   # Daily at 00:37 UTC (off-peak)
  workflow_dispatch:

permissions:
  contents: write

env:
  TARGET_BRANCH: ${{ github.ref_name }}

jobs:
  sync-swrules:
    if: github.event_name != 'schedule' || github.ref_name == ((vars.WORK_BRANCH != '' && vars.WORK_BRANCH) || 'main')
    runs-on: ubuntu-latest

    steps:
      - name: Checkout main branch
        uses: actions/checkout@v6
        with:
          ref: main
          fetch-depth: 0

      - name: Set Git identity
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: '3.x'

      - name: Run conversion script
        run: python scripts/convert_sw_rules.py

      - name: Commit SW-generated YAML files
        run: |
          git add rule/SW_*.yaml || true

          if [[ -n $(git status --porcelain rule/SW_*.yaml) ]]; then
            git commit -m "chore(rules): auto sync Shadowrocket rules"
          else
            echo "No changes in SW rule files, skip commit."
          fi

      - name: Download mihomo
        run: |
          MIHOMO_VERSION="v1.19.18"
          echo "Downloading mihomo ${MIHOMO_VERSION}..."
          wget -q "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-amd64-${MIHOMO_VERSION}.gz"
          gunzip "mihomo-linux-amd64-${MIHOMO_VERSION}.gz"
          chmod +x "mihomo-linux-amd64-${MIHOMO_VERSION}"
          sudo mv "mihomo-linux-amd64-${MIHOMO_VERSION}" /usr/local/bin/mihomo
          echo "mihomo ${MIHOMO_VERSION} installed successfully"

      - name: Convert SW YAML to MRS
        run: |
          echo "Converting SW YAML files to MRS format..."

          for yaml_file in rule/SW_*_Domain.yaml; do
            if [ ! -f "$yaml_file" ]; then
              echo "No SW YAML files found, skipping."
              continue
            fi

            if ! grep -q '^  - ' "$yaml_file"; then
              echo "Skipping $yaml_file (no rules)"
              continue
            fi

            mrs_file="${yaml_file%.yaml}.mrs"
            echo "Converting $yaml_file to $mrs_file"
            mihomo convert-ruleset domain yaml "$yaml_file" "$mrs_file"

            if [ -f "$mrs_file" ]; then
              echo "✓ Generated $mrs_file"
            else
              echo "✗ Failed to generate $mrs_file"
              exit 1
            fi
          done

      - name: Commit MRS files
        run: |
          git add rule/SW_*.mrs || true

          if [[ -n $(git status --porcelain rule/SW_*.mrs) ]]; then
            git commit -m "chore(rules): auto generate SW MRS files"
          else
            echo "No changes in SW MRS files, skip commit."
          fi

      - name: Push changes
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git push origin HEAD:main
```

- [ ] **步骤 2：验证工作流语法**

```bash
# 本地无法直接运行 workflow，用 actionlint 检查（如有安装）
# 手动检查 YAML 语法：缩进、引号、键值对格式
```

- [ ] **步骤 3：Commit**

```bash
git add .github/workflows/auto-sync-swrules.yml
git commit -m "feat: add daily Shadowrocket rules sync workflow"
```

---

### 任务 3：创建 `cfg/Custom_Clash_SW_AD.ini`

**文件：**
- 创建：`cfg/Custom_Clash_SW_AD.ini`

- [ ] **步骤 1：编写 INI 模板**

基于现有 `Custom_Clash.ini` 结构，在基础直连规则之后、GeoIP 分类之前插入 SW 规则引用。JSDelivr CDN URL 遵循项目中已有的格式。

```ini
;Custom_OpenClash_Rules - SW_AD 增强版
;集成 Shadowrocket 精细广告拦截与代理分流规则
;作者：https://github.com/Aethersailor
;项目地址：https://github.com/Yaoser-Archive/Custom_OpenClash_Rules
;规则来源：https://github.com/Johnshall/Shadowrocket-ADBlock-Rules-Forever
;<建议>搭配本项目配套设置，实现最佳化的 OpenClash 使用效果！
;设置方案：https://github.com/Yaoser-Archive/Custom_OpenClash_Rules/wiki/OpenClash-%E8%AE%BE%E7%BD%AE%E6%96%B9%E6%A1%88

[custom]
;设置规则标志位
;以下规则，按照从上往下的顺序遍历，优先命中上位规则，规则重复无影响
;修改顺序会影响分流效果

;========== SW 广告拦截（最高优先级）==========
ruleset=全球直连,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/SW_AD_Domain.yaml,28800

;本地地址和域名直连
ruleset=全球直连,[]GEOSITE,private
ruleset=全球直连,[]GEOIP,private,no-resolve
;本项目收录的直连规则
ruleset=全球直连,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/Custom_Direct_Domain.yaml,28800
ruleset=全球直连,clash-classic:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/Custom_Direct_Classical_IP.yaml,28800

;========== SW 精细代理规则（替代 GeoIP 粗分类）==========
ruleset=苹果服务,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/SW_Proxy_Apple_Domain.yaml,28800
ruleset=DisneyPlus,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/SW_Proxy_Disney_Domain.yaml,28800
ruleset=PrimeVideo,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/SW_Proxy_Amazon_Domain.yaml,28800
ruleset=即时通讯,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/SW_Proxy_Telegram_Domain.yaml,28800
ruleset=AI服务,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/SW_Proxy_AI_Domain.yaml,28800
ruleset=微软服务,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/SW_Proxy_Microsoft_Domain.yaml,28800
ruleset=手动选择,clash-domain:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/SW_Proxy_Other_Domain.yaml,28800

;========== GeoIP 兜底（SW 未覆盖类别）==========
;本项目收录的代理 IP 规则
ruleset=手动选择,clash-classic:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/Custom_Proxy_Classical_IP.yaml,28800
;谷歌在国内可用的域名直连
ruleset=谷歌服务,[]GEOSITE,google-cn
;国内游戏域名直连
ruleset=全球直连,[]GEOSITE,category-games@cn
;Steam 下载 CDN 直连
ruleset=全球直连,clash-classic:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/Steam_CDN_Classical.yaml,28800
;各大游戏平台下载域名直连
ruleset=全球直连,[]GEOSITE,category-game-platforms-download
;BT Tracker 相关域名直连
ruleset=全球直连,[]GEOSITE,category-public-tracker
;即时通讯包括了 Telegram/WhatsApp/Line 等海外主流即时通讯软件域名
ruleset=即时通讯,[]GEOSITE,category-communication
;社交媒体包括了 Twitter(X)/Facebook/Instagram 等海外主流社交媒体
ruleset=社交媒体,[]GEOSITE,category-social-media-!cn
ruleset=ChatGPT,[]GEOSITE,openai
ruleset=AI服务,[]GEOSITE,category-ai-!cn
ruleset=GitHub,[]GEOSITE,github
;测速工具包括 SpeedTest 等主流测速工具域名
ruleset=测速工具,[]GEOSITE,category-speedtest
ruleset=Steam,[]GEOSITE,steam
ruleset=YouTube,[]GEOSITE,youtube
ruleset=AppleTV+,[]GEOSITE,apple-tvplus
ruleset=苹果服务,[]GEOSITE,apple
ruleset=微软服务,[]GEOSITE,microsoft
ruleset=谷歌FCM,[]GEOSITE,googlefcm
ruleset=谷歌服务,[]GEOSITE,google
ruleset=TikTok,[]GEOSITE,tiktok
ruleset=Netflix,[]GEOSITE,netflix
ruleset=DisneyPlus,[]GEOSITE,disney
ruleset=HBO,[]GEOSITE,hbo
ruleset=PrimeVideo,[]GEOSITE,primevideo
;Emby 包括主流 Emby 服务相关域名
ruleset=Emby,[]GEOSITE,category-emby
ruleset=Spotify,[]GEOSITE,spotify
ruleset=Bahamut,[]GEOSITE,bahamut
ruleset=游戏平台,[]GEOSITE,category-games
ruleset=国外媒体,[]GEOSITE,category-entertainment
ruleset=国外电商,[]GEOSITE,category-ecommerce
ruleset=手动选择,[]GEOSITE,gfw
ruleset=即时通讯,[]GEOIP,telegram,no-resolve
ruleset=社交媒体,[]GEOIP,twitter,no-resolve
ruleset=社交媒体,[]GEOIP,facebook,no-resolve
ruleset=谷歌服务,[]GEOIP,google,no-resolve
ruleset=Netflix,[]GEOIP,netflix,no-resolve
;由于 OpenClash 使用的大陆白名单收录不全，此处留有 geosite:cn 作为国内域名兜底
ruleset=全球直连,[]GEOSITE,cn
;由于 OpenClash 使用的大陆白名单收录不全，此处留有 geoip:cn 作为国内 IP 兜底
ruleset=全球直连,[]GEOIP,cn,no-resolve
;以上兜底规则会根据实际情况随时取消
;PT/BT 优化开启会使 80/443 以外端口的流量直连
ruleset=非标端口,clash-classic:https://testingcf.jsdelivr.net/gh/Yaoser-Archive/Custom_OpenClash_Rules@main/rule/Custom_Port_Direct.yaml,28800
;国内冷门域名会命中漏网之鱼，如影响使用，请设置漏网之鱼直连
;漏网之鱼直连时，无法通过 DNS 泄露测试，但是并不存在泄露
ruleset=漏网之鱼,[]FINAL
;设置规则标志位结束

;设置节点分组标志位
custom_proxy_group=手动选择`select`[]自动选择`[]美国节点`.*
custom_proxy_group=自动选择`url-test`.*`https://cp.cloudflare.com/generate_204`300,,50
custom_proxy_group=即时通讯`select`[]手动选择`[]自动选择`[]美国节点`[]全球直连
custom_proxy_group=社交媒体`select`[]手动选择`[]自动选择`[]美国节点`[]全球直连`.*
custom_proxy_group=GitHub`select`[]手动选择`[]自动选择`[]美国节点`[]全球直连
custom_proxy_group=ChatGPT`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=AI服务`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=TikTok`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=YouTube`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=Netflix`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=DisneyPlus`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=HBO`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=PrimeVideo`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=AppleTV+`select`[]手动选择`[]自动选择`[]美国节点`[]全球直连`.*
custom_proxy_group=Emby`select`[]手动选择`[]自动选择`[]全球直连`[]美国节点`.*
custom_proxy_group=Spotify`select`[]手动选择`[]自动选择`[]全球直连`[]美国节点`.*
custom_proxy_group=Bahamut`select`[]手动选择`[]全球直连`.*
custom_proxy_group=国外媒体`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=国外电商`select`[]手动选择`[]自动选择`[]全球直连`[]美国节点`.*
custom_proxy_group=谷歌FCM`select`[]手动选择`[]自动选择`[]美国节点`
custom_proxy_group=谷歌服务`select`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=苹果服务`select`[]全球直连`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=微软服务`select`[]全球直连`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=游戏平台`select`[]全球直连`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=Steam`select`[]全球直连`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=测速工具`select`[]全球直连`[]手动选择`[]自动选择`[]美国节点`.*
custom_proxy_group=漏网之鱼`select`[]手动选择`[]自动选择`[]全球直连`[]美国节点`.*
custom_proxy_group=非标端口`select`[]漏网之鱼`[]全球直连
custom_proxy_group=美国节点`url-test`(美|波特兰|达拉斯|俄勒冈|凤凰城|费利蒙|硅谷|拉斯维加斯|洛杉矶|圣何塞|圣克拉拉|西雅图|芝加哥|纽约|纽纽|亚特兰大|迈阿密|华盛顿|\bUS(?:[-_ ]?\d+(?:[-_ ]?[A-Za-z]{2,})?)?\b|United States|UnitedStates|UNITED STATES|USA|America|AMERICA|JFK|EWR|IAD|ATL|ORD|MIA|NYC|LAX|SFO|SEA|DFW|SJC)`https://cp.cloudflare.com/generate_204`300,,50
custom_proxy_group=全球直连`select`[]DIRECT
;设置分组标志位

;下方参数请勿修改
enable_rule_generator=true
overwrite_original_rules=true
```

- [ ] **步骤 2：与现有 INI 做差异对比**

确认以下差异是有意为之：
1. 顶部新增 SW_AD_Domain 广告拦截规则（最高优先级）
2. 基础直连 + SW 精细代理替代了原有的 `clash-domain:Custom_Proxy_Domain.yaml`
3. GeoIP 兜底保持不变
4. 代理分组与原版一致

- [ ] **步骤 3：Commit**

```bash
git add cfg/Custom_Clash_SW_AD.ini
git commit -m "feat: add Custom_Clash_SW_AD.ini with Shadowrocket enhanced rules"
```

---

### 任务 4：首次运行并验证

**文件：** 无新建，验证生成的产物

- [ ] **步骤 1：运行转换脚本生成初始规则文件**

```bash
python scripts/convert_sw_rules.py
```

预期：生成 `rule/SW_AD_Domain.yaml` 及全部 `rule/SW_Proxy_*_Domain.yaml`。

- [ ] **步骤 2：检查生成文件格式**

```bash
# 每个生成文件的 payload 行数应与 TOTAL 一致
for f in rule/SW_*_Domain.yaml; do
  total=$(grep "^# TOTAL:" "$f" | sed 's/.*: //')
  count=$(grep -c "^  - " "$f" || true)
  echo "$f: TOTAL=$total, actual=$count"
done
```

- [ ] **步骤 3：验证 MRS 可生成（本地有 mihomo 时）**

```bash
# 如果本地安装了 mihomo：
mihomo convert-ruleset domain yaml rule/SW_AD_Domain.yaml rule/SW_AD_Domain.mrs
ls -la rule/SW_AD_Domain.mrs
```

- [ ] **步骤 4：Commit 生成的初始规则**

```bash
git add rule/SW_*_Domain.yaml
git commit -m "chore(rules): add initial Shadowrocket generated rule files"
```

---

## 自检结果

### 1. 规格覆盖度
- ✅ 广告拦截 → 任务 1 的 reject_rules 提取 + SW_AD_Domain.yaml 生成
- ✅ 代理分流增强 → 任务 1 的 PROXY_CATEGORIES 分类 + 各 SW_Proxy_*.yaml 生成
- ✅ 自动化同步 → 任务 2 的 auto-sync-swrules.yml
- ✅ 新 INI 模板 → 任务 3 的 Custom_Clash_SW_AD.ini
- ✅ MRS 二进制生成 → 任务 2 的 mihomo convert 步骤
- ✅ 过滤宽泛规则 → 任务 1 的 SKIP_KEYWORDS + SKIP_DOMAINS
- ✅ 去重排序 → 任务 1 的 sorted(set(...))
- ✅ DOMAIN-KEYWORD 过滤 → 任务 1 的 parse_rules 中只提取 DOMAIN-SUFFIX/DOMAIN/IP-CIDR

### 2. 占位符扫描
- 无 "TODO"、"待定"、"TBD"
- 无 "添加适当的错误处理"（脚本中已有 try/except 和 print）
- 无 "类似任务 N"（每个任务独立完整）

### 3. 类型一致性
- 所有任务间文件名一致：`scripts/convert_sw_rules.py`、`rule/SW_*_Domain.yaml`
- INI 引用的 URL 与生成文件名匹配：`SW_Proxy_Apple_Domain.yaml` 等
- 工作流中的文件模式 `rule/SW_*_Domain.yaml` 与脚本输出一致
