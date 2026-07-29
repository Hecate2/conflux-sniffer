#!/usr/bin/env python3
"""
方案二：Sniffer IP 频率分布分析
分析 sniffer_records.jsonl 中 first_peer_ip 的出现频率，
揭示区块传播的拓扑结构：哪些 IP 最频繁地最先转发区块。

同时结合身份桥日志（如果可用），为每个 IP 标注对应的 AccountAddress。

Usage:
  python3 ip_frequency_analysis.py <sniffer_records.jsonl> [conflux.log]
"""

import re
import sys
import json
import os
from collections import Counter, defaultdict
from datetime import datetime

# Identity bridge pattern from conflux.log
IDENTITY_BRIDGE_RE = re.compile(
    r'\[POSSNIFFER\] Identity bridge: account_addr=([0-9a-fA-F]{64}), node_id=0x([0-9a-fA-F]+), ip=(.+)'
)

# PoS peer connected
PEER_CONNECTED_RE = re.compile(
    r'\[POSSNIFFER\] PoS peer connected: node_id=0x([0-9a-fA-F]+), ip=(.+)'
)

# Relay peers data
RELAY_PEERS_PATH = "/Users/xinghao/Desktop/conflux-rust/pos_relay_peers.json"


def load_relay_peers():
    peers = {}
    if os.path.exists(RELAY_PEERS_PATH):
        with open(RELAY_PEERS_PATH) as f:
            data = json.load(f)
            for p in data.get("relay_peers", []):
                peers[p["account_address"]] = p
    return peers


def load_identity_map(log_path):
    """Load NodeId→IP and AccountAddress→IP mappings from conflux.log."""
    nodeid_to_ip = {}
    acct_to_ip = {}
    acct_to_nodeid = {}

    if not log_path or not os.path.exists(log_path):
        return nodeid_to_ip, acct_to_ip, acct_to_nodeid

    with open(log_path) as f:
        for line in f:
            if "[POSSNIFFER]" not in line:
                continue

            m = IDENTITY_BRIDGE_RE.search(line)
            if m:
                acct = m.group(1).lower()
                nid = m.group(2).lower()
                ip = m.group(3).strip()
                # Strip port from IP for matching
                ip_no_port = ip.rsplit(":", 1)[0] if ":" in ip else ip
                acct_to_ip[acct] = ip_no_port
                acct_to_nodeid[acct] = nid
                nodeid_to_ip[nid] = ip_no_port
                continue

            m = PEER_CONNECTED_RE.search(line)
            if m:
                nid = m.group(1).lower()
                ip = m.group(2).strip()
                ip_no_port = ip.rsplit(":", 1)[0] if ":" in ip else ip
                if nid not in nodeid_to_ip:
                    nodeid_to_ip[nid] = ip_no_port

    return nodeid_to_ip, acct_to_ip, acct_to_nodeid


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 ip_frequency_analysis.py <sniffer_records.jsonl> [conflux.log]")
        sys.exit(1)

    sniffer_path = sys.argv[1]
    log_path = sys.argv[2] if len(sys.argv) > 2 else None

    print("=" * 80)
    print("方案二：Sniffer IP 频率分布分析")
    print("=" * 80)

    relay_peers = load_relay_peers()
    nodeid_to_ip, acct_to_ip, acct_to_nodeid = load_identity_map(log_path)

    # Build reverse maps
    ip_to_nodeid = {v: k for k, v in nodeid_to_ip.items()}
    ip_to_acct = {v: k for k, v in acct_to_ip.items()}

    print(f"\n  Sniffer 记录: {sniffer_path}")
    print(f"  身份桥映射: {len(acct_to_ip)} 个 AccountAddress")
    print(f"  NodeId→IP 映射: {len(nodeid_to_ip)}")

    # Load and analyze sniffer records
    ip_counter = Counter()
    nodeid_counter = Counter()
    ip_to_nodeids = defaultdict(set)
    total_records = 0
    ts_list = []

    with open(sniffer_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ip = rec.get("first_peer_ip", "unknown")
                nid = rec.get("first_peer_node_id", "unknown")
                if nid.startswith("0x"):
                    nid = nid[2:].lower()

                ip_counter[ip] += 1
                nodeid_counter[nid] += 1
                ip_to_nodeids[ip].add(nid)
                total_records += 1

                ts_ms = rec.get("first_seen_at_ms") or rec.get("first_seen_at")
                if ts_ms:
                    # Normalize: if > 1e12 it's in milliseconds, otherwise seconds
                    # Store as normalized seconds for consistent comparison
                    if ts_ms > 1e12:
                        ts_list.append(ts_ms / 1000)
                    else:
                        ts_list.append(ts_ms)
            except json.JSONDecodeError:
                continue

    print(f"  总记录数: {total_records}")
    if ts_list:
        first_ts = datetime.fromtimestamp(min(ts_list)).strftime('%Y-%m-%d %H:%M:%S')
        last_ts = datetime.fromtimestamp(max(ts_list)).strftime('%Y-%m-%d %H:%M:%S')
        print(f"  时间范围: {first_ts} ~ {last_ts}")

    # === Section 1: IP frequency distribution ===
    print("\n" + "=" * 80)
    print("[1] IP 频率分布（哪些 IP 最先转发区块）")
    print("=" * 80)

    print(f"\n  {'排名':>4} {'IP':<22} {'区块数':>8} {'占比':>8} {'NodeId':>20} {'AccountAddress':>20} {'角色':>8}")
    print(f"  {'-'*4} {'-'*22} {'-'*8} {'-'*8} {'-'*20} {'-'*20} {'-'*8}")

    for rank, (ip, count) in enumerate(ip_counter.most_common(50), 1):
        pct = count / total_records * 100
        # Try to find NodeId for this IP
        nid = ip_to_nodeid.get(ip, "")
        nid_short = nid[:16] + "..." if nid else "---"
        # Try to find AccountAddress
        acct = ip_to_acct.get(ip, "")
        acct_short = acct[:16] + "..." if acct else "---"
        # Check if this peer is a known relay peer
        role = "---"
        if acct:
            rp = relay_peers.get(acct, {})
            if rp.get("is_validator"):
                role = "验证者"
            elif acct in relay_peers:
                role = "非验证者"

        print(f"  {rank:>4} {ip:<22} {count:>8} {pct:>7.1f}% {nid_short:>20} {acct_short:>20} {role:>8}")

    # === Section 2: Top IPs cumulative distribution ===
    print("\n" + "=" * 80)
    print("[2] 累计覆盖率（前 N 个 IP 覆盖了多少比例的区块）")
    print("=" * 80)

    cumulative = 0
    print(f"\n  {'前N个IP':>8} {'累计区块数':>10} {'累计覆盖率':>10}")
    print(f"  {'-'*8} {'-'*10} {'-'*10}")
    for i, (ip, count) in enumerate(ip_counter.most_common(20), 1):
        cumulative += count
        pct = cumulative / total_records * 100
        print(f"  {i:>8} {cumulative:>10} {pct:>9.1f}%")

    # === Section 3: NodeId frequency ===
    print("\n" + "=" * 80)
    print("[3] NodeId 频率分布（仅显示完整 NodeId）")
    print("=" * 80)

    # Filter out truncated NodeIds (old format)
    full_nid_counter = Counter()
    truncated_count = 0
    for nid, count in nodeid_counter.items():
        if len(nid) == 128:  # Full 64-byte hex
            full_nid_counter[nid] = count
        else:
            truncated_count += count

    if truncated_count > 0:
        print(f"\n  注意: {truncated_count} 条记录使用了截断的 NodeId（旧格式），已排除。")

    print(f"\n  {'排名':>4} {'NodeId':<66} {'区块数':>8} {'IP':>18} {'AccountAddr':>20}")
    print(f"  {'-'*4} {'-'*66} {'-'*8} {'-'*18} {'-'*20}")

    for rank, (nid, count) in enumerate(full_nid_counter.most_common(30), 1):
        ip = nodeid_to_ip.get(nid, "?")
        acct = ""
        for a, n in acct_to_nodeid.items():
            if n == nid:
                acct = a
                break
        acct_short = acct[:16] + "..." if acct else "---"
        role = ""
        if acct:
            rp = relay_peers.get(acct, {})
            if rp.get("is_validator"):
                role = " (验证者)"
            elif acct in relay_peers:
                role = " (非验证者)"
        print(f"  {rank:>4} {nid} {count:>8} {ip:>18} {acct_short:>20}{role}")

    # === Section 4: Summary ===
    print("\n" + "=" * 80)
    print("[4] 总结")
    print("=" * 80)
    print(f"""
  总区块记录数: {total_records}
  唯一 IP 数: {len(ip_counter)}
  唯一完整 NodeId 数: {len(full_nid_counter)}
  前5个 IP 覆盖率: {sum(c for _, c in ip_counter.most_common(5)) / total_records * 100:.1f}%
  前10个 IP 覆盖率: {sum(c for _, c in ip_counter.most_common(10)) / total_records * 100:.1f}%

  高频 IP 很可能是：
  1. 直接连接了 PoS 验证者的全节点
  2. 验证者本身运行的 PoW 节点
  3. 区块转发路径上的关键中继节点
""")

    # Save report
    output_path = "/Users/xinghao/Desktop/conflux-rust/ip_frequency_report.json"
    report = {
        "total_records": total_records,
        "unique_ips": len(ip_counter),
        "unique_nodeids": len(full_nid_counter),
        "ip_distribution": [
            {
                "ip": ip,
                "count": count,
                "percentage": count / total_records * 100,
                "node_id": ip_to_nodeid.get(ip, ""),
                "account_address": ip_to_acct.get(ip, ""),
            }
            for ip, count in ip_counter.most_common(50)
        ],
    }
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  报告已保存至: {output_path}")


if __name__ == "__main__":
    main()
