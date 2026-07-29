#!/usr/bin/env python3
"""
方案一：PoW pivot hash 交叉匹配
从 PoS 日志中提取 pivot block hash（PoW 区块哈希），
与 sniffer_records.jsonl 中的 block_hash 进行匹配。

匹配成功即可知道：某个 PoS 验证者投票/提议的 PoW pivot 区块，
是由哪个 IP 最先转发给 sniffer 的。

Usage:
  python3 pivot_hash_match.py <pos_log> <sniffer_records.jsonl>
"""

import re
import sys
import json
import os
from collections import defaultdict, Counter
from datetime import datetime

# PoS log patterns
# ReceiveVote line contains: pivot: Some(PivotBlockDecision { height: N, block_hash: 0xHASH })
PIVOT_RE = re.compile(
    r'pivot: Some\(PivotBlockDecision \{ height: (\d+), block_hash: 0x([0-9a-f]{64}) \}'
)

# ReceiveProposal line: {"block_hash":"HASH","event":"ReceiveProposal","remote_peer":"ACCT",...}
PROPOSAL_RE = re.compile(
    r'"event":"ReceiveProposal","remote_peer":"([0-9a-f]{64})"'
)
PROPOSAL_HASH_RE = re.compile(
    r'"block_hash":"([0-9a-f]{64})".*"event":"ReceiveProposal"'
)

# ReceiveVote: remote_peer is the voter's AccountAddress
VOTE_RE = re.compile(
    r'"event":"ReceiveVote","remote_peer":"([0-9a-f]{64})"'
)

# Timestamp from PoS log: 2026-07-22T04:03:56.078462Z
POS_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')

# Validator set from pos_relay_peers.json
RELAY_PEERS_PATH = "/Users/xinghao/Desktop/conflux-rust/pos_relay_peers.json"


def load_relay_peers():
    peers = {}
    if os.path.exists(RELAY_PEERS_PATH):
        with open(RELAY_PEERS_PATH) as f:
            data = json.load(f)
            for p in data.get("relay_peers", []):
                peers[p["account_address"]] = p
    return peers


def load_sniffer_records(filepath):
    records = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                bh = rec.get("block_hash", "").lower()
                if bh.startswith("0x"):
                    bh = bh[2:]
                records[bh] = rec
            except json.JSONDecodeError:
                continue
    return records


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 pivot_hash_match.py <pos_log> <sniffer_records.jsonl>")
        sys.exit(1)

    pos_log = sys.argv[1]
    sniffer_path = sys.argv[2]

    print("=" * 80)
    print("方案一：PoW Pivot Hash 交叉匹配 (PoS 日志 ↔ Sniffer 记录)")
    print("=" * 80)

    relay_peers = load_relay_peers()
    sniffer_records = load_sniffer_records(sniffer_path)

    print(f"\n  PoS 日志: {pos_log}")
    print(f"  Sniffer 记录: {sniffer_path}")
    print(f"  已知 relay peers: {len(relay_peers)}")
    print(f"  Sniffer 区块记录: {len(sniffer_records)}")

    # Extract pivot hashes from PoS log
    pivot_hashes = {}       # pow_hash -> {height, voters: set(), proposers: set(), ts_list: []}
    proposals = []          # (ts, proposer_acct, pos_block_hash)
    pos_block_to_pivot = {} # pos_block_hash -> pow_pivot_hash

    line_count = 0
    with open(pos_log) as f:
        for line in f:
            line_count += 1
            if line_count % 100000 == 0:
                print(f"  已处理 {line_count} 行...")

            ts_m = POS_TS_RE.match(line)
            ts_str = ts_m.group(1) if ts_m else ""

            # Extract pivot hash from ReceiveVote lines
            pivot_m = PIVOT_RE.search(line)
            if pivot_m:
                height = int(pivot_m.group(1))
                pow_hash = pivot_m.group(2)

                if pow_hash not in pivot_hashes:
                    pivot_hashes[pow_hash] = {
                        "height": height,
                        "voters": set(),
                        "proposers": set(),
                        "first_ts": ts_str,
                        "last_ts": ts_str,
                    }
                else:
                    pivot_hashes[pow_hash]["last_ts"] = ts_str

                # Extract voter AccountAddress
                vote_m = VOTE_RE.search(line)
                if vote_m:
                    voter = vote_m.group(1)
                    pivot_hashes[pow_hash]["voters"].add(voter)

            # Extract ReceiveProposal events
            if '"ReceiveProposal"' in line:
                prop_m = PROPOSAL_RE.search(line)
                prop_hash_m = PROPOSAL_HASH_RE.search(line)
                if prop_m and prop_hash_m:
                    proposer = prop_m.group(1)
                    pos_block_hash = prop_hash_m.group(1)
                    proposals.append((ts_str, proposer, pos_block_hash))

                    # Try to find pivot hash on same line
                    if pivot_m:
                        pos_block_to_pivot[pos_block_hash] = pivot_m.group(2)
                        pivot_hashes[pivot_m.group(2)]["proposers"].add(proposer)

    print(f"\n  PoS 日志总行数: {line_count}")
    print(f"  唯一 PoW pivot hash: {len(pivot_hashes)}")
    print(f"  ReceiveProposal 事件: {len(proposals)}")
    print(f"  PoS block → PoW pivot 映射: {len(pos_block_to_pivot)}")

    # Show time ranges
    if pivot_hashes:
        all_ts = [p["first_ts"] for p in pivot_hashes.values()]
        all_ts += [p["last_ts"] for p in pivot_hashes.values()]
        print(f"  PoS 日志时间范围: {min(all_ts)} ~ {max(all_ts)}")

    if sniffer_records:
        ts_list = []
        for rec in sniffer_records.values():
            ts_ms = rec.get("first_seen_at_ms") or rec.get("first_seen_at")
            if ts_ms:
                if ts_ms > 1e12:  # milliseconds
                    ts_list.append(datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%dT%H:%M:%S'))
                else:  # seconds
                    ts_list.append(datetime.fromtimestamp(ts_ms).strftime('%Y-%m-%dT%H:%M:%S'))
        if ts_list:
            print(f"  Sniffer 记录时间范围: {min(ts_list)} ~ {max(ts_list)}")

    # Cross-match
    print("\n" + "=" * 80)
    print("[1] PoW Pivot Hash 交叉匹配结果")
    print("=" * 80)

    matched = []
    for pow_hash, info in pivot_hashes.items():
        if pow_hash in sniffer_records:
            sr = sniffer_records[pow_hash]
            sniffer_ip = sr.get("first_peer_ip", "unknown")
            sniffer_nid = sr.get("first_peer_node_id", "unknown")
            sniffer_ts = sr.get("first_seen_at_ms") or sr.get("first_seen_at", 0)

            voters_info = []
            for v in info["voters"]:
                rp = relay_peers.get(v, {})
                is_val = "验证者" if rp.get("is_validator") else "非验证者"
                voters_info.append({
                    "account": v,
                    "role": is_val,
                    "proposals": rp.get("proposal_count", 0),
                })

            proposers_info = []
            for p in info["proposers"]:
                rp = relay_peers.get(p, {})
                is_val = "验证者" if rp.get("is_validator") else "非验证者"
                proposers_info.append({
                    "account": p,
                    "role": is_val,
                    "proposals": rp.get("proposal_count", 0),
                })

            matched.append({
                "pow_hash": pow_hash,
                "height": info["height"],
                "pos_first_ts": info["first_ts"],
                "pos_last_ts": info["last_ts"],
                "sniffer_ip": sniffer_ip,
                "sniffer_node_id": sniffer_nid,
                "sniffer_ts": sniffer_ts,
                "voters": voters_info,
                "proposers": proposers_info,
                "voter_count": len(info["voters"]),
                "proposer_count": len(info["proposers"]),
            })

    print(f"\n  匹配到的区块数: {len(matched)} / {len(pivot_hashes)}")

    if matched:
        print(f"\n  {'PoW Hash':<20} {'Height':>12} {'Sniffer IP':>18} {'投票者':>6} {'提议者':>6} {'验证者?':>8}")
        print(f"  {'-'*20} {'-'*12} {'-'*18} {'-'*6} {'-'*6} {'-'*8}")
        for m in sorted(matched, key=lambda x: x["height"])[:50]:
            has_validator = any(v["role"] == "验证者" for v in m["voters"])
            val_mark = "是" if has_validator else "否"
            print(f"  {m['pow_hash'][:18]}... {m['height']:>12} {m['sniffer_ip']:>18} {m['voter_count']:>6} {m['proposer_count']:>6} {val_mark:>8}")

        # Detailed view of first 10 matches
        print(f"\n  --- 前10个匹配区块的详细信息 ---")
        for m in sorted(matched, key=lambda x: x["height"])[:10]:
            print(f"\n  PoW Hash: 0x{m['pow_hash']}")
            print(f"  Height:   {m['height']}")
            print(f"  Sniffer:  IP={m['sniffer_ip']}, NodeId={m['sniffer_node_id'][:16]}...")
            if m['sniffer_ts']:
                ts_val = m['sniffer_ts']
                if ts_val > 1e12:
                    ts_str = datetime.fromtimestamp(ts_val / 1000).strftime('%Y-%m-%d %H:%M:%S')
                else:
                    ts_str = datetime.fromtimestamp(ts_val).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  Sniffer 时间: {ts_str}")
            print(f"  PoS 时间: {m['pos_first_ts']} ~ {m['pos_last_ts']}")
            print(f"  投票者 ({m['voter_count']}):")
            for v in m["voters"][:5]:
                print(f"    {v['account'][:16]}... ({v['role']}, 提案数={v['proposals']})")
            if m["proposers"]:
                print(f"  提议者 ({m['proposer_count']}):")
                for p in m["proposers"][:3]:
                    print(f"    {p['account'][:16]}... ({p['role']}, 提案数={p['proposals']})")
    else:
        print("\n  ⚠ 未找到匹配的区块。")
        print("  原因分析：PoS 日志和 Sniffer 记录的时间范围不重叠。")
        print("  解决方案：同时运行 PoS 节点和 Sniffer 节点，确保时间重叠。")

    # Sniffer IP → matched block count
    if matched:
        print("\n" + "=" * 80)
        print("[2] Sniffer IP 转发匹配区块统计")
        print("=" * 80)
        ip_counter = Counter()
        for m in matched:
            ip_counter[m["sniffer_ip"]] += 1
        print(f"\n  {'IP':<22} {'匹配区块数':>10}")
        print(f"  {'-'*22} {'-'*10}")
        for ip, count in ip_counter.most_common(20):
            print(f"  {ip:<22} {count:>10}")

    # Save report
    output_path = "/Users/xinghao/Desktop/conflux-rust/pivot_hash_match_report.json"
    report = {
        "pos_log": pos_log,
        "sniffer_records": sniffer_path,
        "total_pivot_hashes": len(pivot_hashes),
        "total_sniffer_records": len(sniffer_records),
        "matched_blocks": len(matched),
        "matches": [
            {
                "pow_hash": m["pow_hash"],
                "height": m["height"],
                "sniffer_ip": m["sniffer_ip"],
                "sniffer_node_id": m["sniffer_node_id"],
                "voter_count": m["voter_count"],
                "proposer_count": m["proposer_count"],
            }
            for m in matched
        ],
    }
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n  报告已保存至: {output_path}")


if __name__ == "__main__":
    main()
