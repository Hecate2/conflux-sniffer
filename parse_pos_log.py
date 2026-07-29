#!/usr/bin/env python3
"""
解析 bootnode 的 PoS 日志，输出类似 sniffer_records.jsonl 格式的 JSONL 文件。

从 pos.log 中提取两类事件：
  1. ReceiveProposal — PoS 区块提议（block_hash 是 PoS 区块哈希）
  2. ReceiveVote      — PoS 投票（内含 PoW pivot block_hash）

对每个唯一的 block_hash，只记录首次收到的时间和来源 peer，
与 sniffer_records.jsonl 的"首达"语义一致。

同时从 conflux.log 中加载身份桥映射（AccountAddress → NodeId → IP），
为每条记录补充 IP 和 NodeId 字段。

输出文件：
  run/pos_proposal_records.jsonl  — PoS 区块提议记录
  run/pos_pivot_records.jsonl     — PoW pivot hash 记录（可与 sniffer_records.jsonl 交叉匹配）

Usage:
  python3 parse_pos_log.py <pos.log> [conflux.log]

Example:
  python3 parse_pos_log.py pos.log run/log/conflux.log
"""

import re
import sys
import json
import os
from collections import Counter
from datetime import datetime, timezone

# ── 正则表达式 ──────────────────────────────────────────────────────────────

# PoS 日志时间戳：2026-07-26T02:51:27.810030Z
TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)')

# ReceiveProposal 行：
# {"block_hash":"...","block_parent_hash":"...","epoch":...,"event":"ReceiveProposal","remote_peer":"...","round":...}
# 字段顺序不固定，故用独立正则分别提取
PROPOSAL_HASH_RE = re.compile(r'"block_hash":"([0-9a-f]{64})"')
PROPOSAL_PEER_RE = re.compile(r'"remote_peer":"([0-9a-f]{64})"')
PROPOSAL_EPOCH_RE = re.compile(r'"epoch":(\d+)')
PROPOSAL_ROUND_RE = re.compile(r'"round":(\d+)')

# ReceiveVote 行中的 pivot hash：
# pivot: Some(PivotBlockDecision { height: 152910240, block_hash: 0xab73438b... })
PIVOT_RE = re.compile(
    r'pivot: Some\(PivotBlockDecision \{ height: (\d+), block_hash: 0x([0-9a-f]{64}) \}'
)

# ReceiveVote 行中的 remote_peer（投票者 AccountAddress）
VOTE_PEER_RE = re.compile(
    r'"event":"ReceiveVote","remote_peer":"([0-9a-f]{64})"'
)
VOTE_EPOCH_RE = re.compile(r'"vote_epoch":(\d+)')
VOTE_ROUND_RE = re.compile(r'"vote_round":(\d+)')

# 身份桥日志：[POSSNIFFER] Identity bridge: account_addr=..., node_id=0x..., ip=...
IDENTITY_BRIDGE_RE = re.compile(
    r'\[POSSNIFFER\] Identity bridge: account_addr=([0-9a-fA-F]{64}), '
    r'node_id=0x([0-9a-fA-F]+), ip=(.+)'
)


# ── 工具函数 ────────────────────────────────────────────────────────────────

def parse_ts_to_ms(ts_str: str) -> int:
    """将 ISO 8601 时间戳转换为毫秒级 Unix 时间戳。

    输入格式：2026-07-26T02:51:27.810030Z
    """
    # Python 的 datetime.fromisoformat 在 3.11+ 支持 Z 后缀
    # 为兼容旧版本，手动替换 Z 为 +00:00
    ts_str = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts_str)
    return int(dt.timestamp() * 1000)


def load_identity_map(log_path: str) -> dict:
    """从 conflux.log 中加载身份桥映射。

    返回: {account_address(lowercase): {"node_id": "...", "ip": "..."}}
    """
    mapping = {}
    if not log_path or not os.path.exists(log_path):
        return mapping

    with open(log_path, errors="replace") as f:
        for line in f:
            if "[POSSNIFFER]" not in line or "Identity bridge" not in line:
                continue
            m = IDENTITY_BRIDGE_RE.search(line)
            if m:
                acct = m.group(1).lower()
                nid = m.group(2).lower()
                ip_raw = m.group(3).strip()
                # 去掉端口号，只保留 IP
                ip = ip_raw.rsplit(":", 1)[0] if ":" in ip_raw else ip_raw
                mapping[acct] = {"node_id": nid, "ip": ip}

    return mapping


# ── 主逻辑 ──────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 parse_pos_log.py <pos.log> [conflux.log]")
        sys.exit(1)

    pos_log = sys.argv[1]
    conflux_log = sys.argv[2] if len(sys.argv) > 2 else None

    print("=" * 80)
    print("解析 bootnode PoS 日志 → JSONL 记录")
    print("=" * 80)
    print(f"  PoS 日志: {pos_log}")
    print(f"  conflux 日志: {conflux_log or '(未提供，跳过身份桥映射)'}")

    # 加载身份桥映射
    identity_map = load_identity_map(conflux_log)
    print(f"  身份桥映射: {len(identity_map)} 个 AccountAddress")

    # 数据结构：对每个 block_hash 只保留首次记录
    # proposal_first_seen: {pos_block_hash: {ts_ms, account_addr, epoch, round}}
    # pivot_first_seen:    {pow_pivot_hash:  {ts_ms, account_addr, height, epoch, round}}
    proposal_first_seen = {}
    pivot_first_seen = {}

    # 统计计数
    total_proposals = 0
    total_votes = 0
    total_lines = 0

    print("\n  开始解析...")

    with open(pos_log, errors="replace") as f:
        for line in f:
            total_lines += 1
            if total_lines % 100000 == 0:
                print(f"    已处理 {total_lines} 行...")

            # 提取时间戳
            ts_m = TS_RE.match(line)
            if not ts_m:
                continue
            ts_ms = parse_ts_to_ms(ts_m.group(1))

            # ReceiveProposal
            if '"ReceiveProposal"' in line:
                hash_m = PROPOSAL_HASH_RE.search(line)
                peer_m = PROPOSAL_PEER_RE.search(line)
                epoch_m = PROPOSAL_EPOCH_RE.search(line)
                round_m = PROPOSAL_ROUND_RE.search(line)
                if hash_m and peer_m and epoch_m and round_m:
                    block_hash = hash_m.group(1)
                    account_addr = peer_m.group(1)
                    epoch = int(epoch_m.group(1))
                    round_num = int(round_m.group(1))
                    total_proposals += 1

                    # 只保留首次收到
                    if block_hash not in proposal_first_seen:
                        proposal_first_seen[block_hash] = {
                            "ts_ms": ts_ms,
                            "account_addr": account_addr,
                            "epoch": epoch,
                            "round": round_num,
                        }

            # ReceiveVote
            elif '"ReceiveVote"' in line:
                pivot_m = PIVOT_RE.search(line)
                peer_m = VOTE_PEER_RE.search(line)
                if pivot_m and peer_m:
                    pow_hash = pivot_m.group(2)
                    height = int(pivot_m.group(1))
                    account_addr = peer_m.group(1)
                    total_votes += 1

                    epoch_m = VOTE_EPOCH_RE.search(line)
                    round_m = VOTE_ROUND_RE.search(line)
                    epoch = int(epoch_m.group(1)) if epoch_m else 0
                    round_num = int(round_m.group(1)) if round_m else 0

                    # 只保留首次收到
                    if pow_hash not in pivot_first_seen:
                        pivot_first_seen[pow_hash] = {
                            "ts_ms": ts_ms,
                            "account_addr": account_addr,
                            "height": height,
                            "epoch": epoch,
                            "round": round_num,
                        }

    print(f"\n  解析完成:")
    print(f"    总行数: {total_lines}")
    print(f"    ReceiveProposal 事件: {total_proposals}")
    print(f"    ReceiveVote 事件: {total_votes}")
    print(f"    唯一 PoS 区块哈希: {len(proposal_first_seen)}")
    print(f"    唯一 PoW pivot 哈希: {len(pivot_first_seen)}")

    # ── 输出 PoS 提议记录 ────────────────────────────────────────────────

    proposal_path = "/Users/xinghao/Desktop/conflux-rust/run/pos_proposal_records.jsonl"
    proposal_count = 0
    with open(proposal_path, "a") as f:
        for block_hash, info in sorted(proposal_first_seen.items(), key=lambda x: x[1]["ts_ms"]):
            acct = info["account_addr"]
            identity = identity_map.get(acct, {})
            record = {
                "block_hash": f"0x{block_hash}",
                "first_peer_account_address": acct,
                "first_peer_ip": identity.get("ip", ""),
                "first_peer_node_id": f"0x{identity['node_id']}" if identity.get("node_id") else "",
                "first_seen_at_ms": info["ts_ms"],
                "epoch": info["epoch"],
                "round": info["round"],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            proposal_count += 1

    print(f"\n  PoS 提议记录已追加至: {proposal_path} ({proposal_count} 条)")

    # ── 输出 PoW pivot 记录 ──────────────────────────────────────────────

    pivot_path = "/Users/xinghao/Desktop/conflux-rust/run/pos_pivot_records.jsonl"
    pivot_count = 0
    with open(pivot_path, "a") as f:
        for pow_hash, info in sorted(pivot_first_seen.items(), key=lambda x: x[1]["ts_ms"]):
            acct = info["account_addr"]
            identity = identity_map.get(acct, {})
            record = {
                "block_hash": f"0x{pow_hash}",
                "pivot_height": info["height"],
                "first_peer_account_address": acct,
                "first_peer_ip": identity.get("ip", ""),
                "first_peer_node_id": f"0x{identity['node_id']}" if identity.get("node_id") else "",
                "first_seen_at_ms": info["ts_ms"],
                "epoch": info["epoch"],
                "round": info["round"],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            pivot_count += 1

    print(f"  PoW pivot 记录已追加至: {pivot_path} ({pivot_count} 条)")

    # ── 摘要统计 ──────────────────────────────────────────────────────────

    print("\n" + "=" * 80)
    print("[摘要统计]")
    print("=" * 80)

    if proposal_first_seen:
        ts_list = [v["ts_ms"] for v in proposal_first_seen.values()]
        first = datetime.fromtimestamp(min(ts_list) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        last = datetime.fromtimestamp(max(ts_list) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n  PoS 提议时间范围: {first} ~ {last}")

        # 统计有/无身份映射的比例
        mapped = sum(1 for v in proposal_first_seen.values() if v["account_addr"] in identity_map)
        print(f"  有身份桥映射: {mapped}/{len(proposal_first_seen)} ({mapped/len(proposal_first_seen)*100:.1f}%)")

        # 按 AccountAddress 统计提议数
        from collections import Counter
        acct_counter = Counter(v["account_addr"] for v in proposal_first_seen.values())
        print(f"\n  提议数前 5 的 AccountAddress:")
        for rank, (acct, cnt) in enumerate(acct_counter.most_common(5), 1):
            identity = identity_map.get(acct, {})
            ip = identity.get("ip", "未知")
            print(f"    {rank}. {acct[:16]}...  提议 {cnt} 个区块  IP={ip}")

    if pivot_first_seen:
        ts_list = [v["ts_ms"] for v in pivot_first_seen.values()]
        first = datetime.fromtimestamp(min(ts_list) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        last = datetime.fromtimestamp(max(ts_list) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n  PoW pivot 时间范围: {first} ~ {last}")

        mapped = sum(1 for v in pivot_first_seen.values() if v["account_addr"] in identity_map)
        print(f"  有身份桥映射: {mapped}/{len(pivot_first_seen)} ({mapped/len(pivot_first_seen)*100:.1f}%)")

        # 按 AccountAddress 统计首次提到 pivot 的数量
        acct_counter = Counter(v["account_addr"] for v in pivot_first_seen.values())
        print(f"\n  首次提及 pivot 数前 5 的 AccountAddress:")
        for rank, (acct, cnt) in enumerate(acct_counter.most_common(5), 1):
            identity = identity_map.get(acct, {})
            ip = identity.get("ip", "未知")
            print(f"    {rank}. {acct[:16]}...  首次提及 {cnt} 个 pivot  IP={ip}")

    print()


if __name__ == "__main__":
    main()
