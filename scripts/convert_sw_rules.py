#!/usr/bin/env python3
"""Convert Shadowrocket AD/Proxy rules to OpenClash YAML format.

Downloads sr_cnip_ad.conf from Johnshall's repository,
parses rules by action (Reject/Proxy), classifies proxy rules
into service categories, and generates YAML rule files.

Domain rules are stored as (rule_type, value) tuples to preserve
the distinction between DOMAIN (exact match) and DOMAIN-SUFFIX
(subdomain wildcard). IP-CIDR rules are tracked separately and
excluded from Domain YAML — they are covered by GeoIP fallback.

Usage:
    python scripts/convert_sw_rules.py
"""

import urllib.request
import urllib.error
import os
import re
import sys
from pathlib import Path

# Configuration
SOURCE_URL = "https://johnshall.github.io/Shadowrocket-ADBlock-Rules-Forever/sr_cnip_ad.conf"
RULE_DIR = Path(__file__).resolve().parent.parent / "rule"
REPO_URL = "https://github.com/Yaoser-Archive/Custom_OpenClash_Rules"
SOURCE_REF = "https://github.com/Johnshall/Shadowrocket-ADBlock-Rules-Forever"

# Domain classification patterns for proxy rules.
# (category_name, output_filename, [patterns])
# All patterns use (^|\.) anchor to match domain at start or after a dot,
# preventing substring false-matches while also matching bare domains
# (e.g. "a2z.com" without a leading subdomain).
PROXY_CATEGORIES = [
    ("Apple", "SW_Proxy_Apple", [
        r"(^|\.)apple\.", r"(^|\.)icloud\.", r"(^|\.)itunes\.",
        r"(^|\.)aaplimg\.", r"(^|\.)akadns\.net",
        r"(^|\.)apple\.news", r"(^|\.)apple-dns\.net",
        r"(^|\.)apple\.comscoreresearch", r"(^|\.)apple-mapkit\.com",
    ]),
    ("Disney+", "SW_Proxy_Disney", [
        r"(^|\.)disney\.", r"(^|\.)bamgrid\.", r"(^|\.)dssott\.",
        r"(^|\.)dilcdn\.", r"(^|\.)starwars\.com", r"(^|\.)go\.com",
    ]),
    ("Amazon/PrimeVideo", "SW_Proxy_Amazon", [
        r"(^|\.)amazon\.", r"(^|\.)primevideo\.", r"(^|\.)audible\.",
        r"(^|\.)amzn\.", r"(^|\.)a2z\.", r"(^|\.)amazonaws\.com",
        r"(^|\.)amazonpay\.com", r"(^|\.)imdb\.",
        r"(^|\.)cloudfront\.net", r"(^|\.)kindle\.com",
    ]),
    ("Telegram", "SW_Proxy_Telegram", [
        r"(^|\.)telegram\.", r"^t\.me$", r"(^|\.)tdesktop\.com",
        r"(^|\.)telegra\.ph", r"(^|\.)telesco\.pe",
    ]),
    ("AI Tools", "SW_Proxy_AI", [
        r"(^|\.)copilot\.microsoft\.com", r"(^|\.)devv\.ai",
        r"(^|\.)forefront\.ai", r"(^|\.)github\.dev", r"(^|\.)bing\.com",
    ]),
    ("Microsoft/GitHub", "SW_Proxy_Microsoft", [
        r"(^|\.)microsoft\.com", r"(^|\.)office\.", r"(^|\.)live\.com",
        r"(^|\.)live\.net", r"(^|\.)1drv\.com", r"(^|\.)onedrive\.",
        r"(^|\.)raw\.githubusercontent\.com", r"(^|\.)hockeyapp\.net",
        r"(^|\.)svc\.ms",
    ]),
]

# Broad DOMAIN-KEYWORD rules to skip (too aggressive for subdomain matching)
SKIP_KEYWORDS = {"amazon", "aws"}

# Broad catch-all domains to exclude from precise categories.
# Subdomains (e.g. "cdn.akamaiedge.net") also match via endswith check.
SKIP_DOMAINS = {"akamaiedge.net"}


def download_rules(url: str) -> str:
    """Download rules file and return as text.

    Raises SystemExit on network or HTTP errors.
    """
    print(f"Downloading: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "OpenClash-Rule-Sync/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} — {e.reason}", file=sys.stderr)
        print(f"       URL: {url}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: Network error — {e.reason}", file=sys.stderr)
        print(f"       URL: {url}", file=sys.stderr)
        sys.exit(1)
    print(f"Downloaded {len(content)} bytes, {len(content.splitlines())} lines")
    return content


def parse_rules(content: str) -> tuple:
    """Parse Shadowrocket rules into reject and proxy lists.

    Domain rules are stored as (rule_type, value) tuples to preserve
    the distinction between DOMAIN (exact match) and DOMAIN-SUFFIX
    (subdomain wildcard). IP-CIDR rules are tracked in separate lists
    because they should not appear in domain YAML files.

    Returns:
        (reject_rules, ip_reject_rules, ip_proxy_rules,
         {category_filename: [(rule_type, value), ...]})
    """
    reject_rules = []       # (rule_type, value) for DOMAIN/DOMAIN-SUFFIX
    ip_reject_rules = []    # IP-CIDR values (excluded from domain YAML)
    ip_proxy_rules = []     # IP-CIDR values (excluded from domain YAML)
    proxy_rules = []        # (rule_type, value) for DOMAIN/DOMAIN-SUFFIX
    skipped_keywords = []   # tracked for summary reporting

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

        # Filter overly broad DOMAIN-KEYWORD rules
        if rule_type == "DOMAIN-KEYWORD":
            if value.lower() in SKIP_KEYWORDS:
                skipped_keywords.append(value)
            continue  # DOMAIN-KEYWORD rules not applicable to domain YAML

        if action in ("Reject", "REJECT"):
            if rule_type in ("DOMAIN-SUFFIX", "DOMAIN"):
                reject_rules.append((rule_type, value))
            elif rule_type == "IP-CIDR":
                ip_reject_rules.append(value)
        elif action in ("Proxy", "PROXY"):
            if rule_type in ("DOMAIN-SUFFIX", "DOMAIN"):
                proxy_rules.append((rule_type, value))
            elif rule_type == "IP-CIDR":
                ip_proxy_rules.append(value)

    if skipped_keywords:
        print(f"Skipped {len(skipped_keywords)} broad DOMAIN-KEYWORD rule(s): "
              f"{', '.join(sorted(set(skipped_keywords)))}")

    # Deduplicate by (rule_type, value) — same domain may appear as both
    # DOMAIN and DOMAIN-SUFFIX; those are semantically different and kept.
    reject_rules = sorted(set(reject_rules), key=lambda x: x[1])
    proxy_rules = sorted(set(proxy_rules), key=lambda x: x[1])

    # Classify proxy rules into service categories
    categorized: dict[str, list[tuple[str, str]]] = {}
    for rule_type, domain in proxy_rules:
        cat = classify_proxy_domain(domain)
        if cat not in categorized:
            categorized[cat] = []
        categorized[cat].append((rule_type, domain))

    return reject_rules, ip_reject_rules, ip_proxy_rules, categorized


def classify_proxy_domain(domain: str) -> str:
    """Classify a proxy domain into a category filename stem.

    Broad catch-all domains (e.g. generic CDN) are placed in
    SW_Proxy_Other to avoid polluting precise service categories.
    Subdomains like "cdn.akamaiedge.net" are also caught via
    endswith matching.
    """
    domain_lower = domain.lower()

    # Exclude broad catch-all domains and their subdomains
    if any(domain_lower == d or domain_lower.endswith("." + d)
           for d in SKIP_DOMAINS):
        return "SW_Proxy_Other"

    for _cat_name, filename, patterns in PROXY_CATEGORIES:
        for pattern in patterns:
            if re.search(pattern, domain_lower):
                return filename
    return "SW_Proxy_Other"


def generate_yaml(filename_stem: str, rules: list[tuple[str, str]], action: str,
                  rule_dir: Path) -> None:
    """Generate an OpenClash domain YAML file.

    DOMAIN-SUFFIX rules use '+.domain' prefix (subdomain wildcard).
    DOMAIN rules use bare 'domain' (exact match, no '+' prefix).
    """
    filepath = rule_dir / f"{filename_stem}_Domain.yaml"
    total = len(rules)

    lines = []
    lines.append(f"# Generated from sr_cnip_ad.conf")
    lines.append(f"# SOURCE: {SOURCE_REF}")
    lines.append(f"# REPO: {REPO_URL}")
    lines.append(f"# TOTAL: {total}")
    lines.append(f"# ACTION: {action}")
    lines.append("")
    lines.append("payload:")

    for rule_type, value in rules:
        if rule_type == "DOMAIN-SUFFIX":
            lines.append(f"  - '+.{value}'")
        else:  # DOMAIN (exact match, no subdomain wildcard)
            lines.append(f"  - '{value}'")

    filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Generated: {filepath} ({total} rules)")


def main():
    """Main entry point."""
    print("=" * 60)
    print("Shadowrocket Rules -> OpenClash YAML Converter")
    print("=" * 60)

    # Ensure rule directory exists
    RULE_DIR.mkdir(parents=True, exist_ok=True)

    # Download
    content = download_rules(SOURCE_URL)

    # Parse
    reject_rules, ip_reject_rules, ip_proxy_rules, proxy_categorized = \
        parse_rules(content)

    # Generate AD reject rules from domain rules only.
    # IP-CIDR rules are excluded from domain YAML files because
    # GeoIP-based routing covers them at the Clash level.
    generate_yaml("SW_AD", reject_rules, "REJECT", RULE_DIR)

    # Generate proxy category rules (domain rules only)
    for filename_stem, rules in sorted(proxy_categorized.items()):
        deduped = sorted(set(rules), key=lambda x: x[1])
        generate_yaml(filename_stem, deduped, "PROXY", RULE_DIR)

    # Summary
    total_proxy = sum(len(v) for v in proxy_categorized.values())
    print(f"\nSummary:")
    print(f"  AD Reject rules (domain):   {len(reject_rules)}")
    print(f"  IP-CIDR Reject (skipped):   {len(ip_reject_rules)} "
          f"(covered by GeoIP)")
    print(f"  IP-CIDR Proxy (skipped):    {len(ip_proxy_rules)} "
          f"(covered by GeoIP)")
    print(f"  Proxy rules (domain):       {total_proxy}")
    for filename_stem, rules in sorted(proxy_categorized.items()):
        deduped = len(set(rules))
        suffix_count = sum(1 for rt, _ in rules if rt == "DOMAIN-SUFFIX")
        exact_count = deduped - suffix_count
        print(f"    {filename_stem}: {deduped} "
              f"(SUFFIX={suffix_count}, EXACT={exact_count})")
    print("\nDone.")


if __name__ == "__main__":
    main()
