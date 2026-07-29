#!/usr/bin/env python3
"""
Analyze sniffer logs that contain [POSSNIFFER] entries.
This script processes the new log output from the modified sync_protocol.rs
to build a NodeId → IP mapping for PoS peers and cross-reference with
PoS consensus events (ReceiveProposal/ReceiveVote) and PoW block records.

Usage:
  python3 analyze_possniffer_logs.py <log_file> [sniffer_records.jsonl] [pos_validators_nodeids.txt]

The script will:
1. Extract [POSSNIFFER] peer connection events (NodeId → IP mapping)
2. Extract [POSSNIFFER] consensus message events (sender NodeId + IP)
3. Extract ReceiveProposal/ReceiveVote events (author NodeId + PoW pivot hash)
4. Cross-reference: PoS author NodeId → sender IP → PoW pivot hash
5. Cross-reference with sniffer_records.jsonl (PoW NewBlockHash records)
6. Generate a comprehensive block source tracing report
"""

import re
import sys
import json
import os
from collections import defaultdict, Counter
from datetime import datetime

# Regex patterns
# NOTE: AccountAddress from Rust {:?} is UPPERCASE hex; NodeId from {:#x} is lowercase hex.
# All regexes use [0-9a-fA-F] to match both cases. Addresses are normalized to lowercase
# before comparison with pos_relay_peers.json (which uses lowercase).
PEER_CONNECTED_RE = re.compile(
    r'\[POSSNIFFER\] PoS peer connected: node_id=0x([0-9a-fA-F]+), ip=(.+)'
)

PEER_DISCONNECTED_RE = re.compile(
    r'\[POSSNIFFER\] PoS peer disconnected: node_id=0x([0-9a-fA-F]+)'
)

CONSENSUS_MSG_RE = re.compile(
    r'\[POSSNIFFER\] Consensus msg: node_id=0x([0-9a-fA-F]+), ip=(.+?), msg_id=0x([0-9a-fA-F]+), account_addr=(?:Some\(([0-9a-fA-F]{64})\)|None)'
)

IDENTITY_BRIDGE_RE = re.compile(
    r'\[POSSNIFFER\] Identity bridge: account_addr=([0-9a-fA-F]{64}), node_id=0x([0-9a-fA-F]+), ip=(.+)'
)

# ReceiveProposal: {"event":"ReceiveProposal","remote_peer":"NodeId",...,"block_hash":"..."}
PROPOSAL_RE = re.compile(
    r'"event":"ReceiveProposal","remote_peer":"([0-9a-f]{64})".*?"block_hash":"([0-9a-f]{64})"'
)

# ReceiveVote with pivot: pivot: Some(PivotBlockDecision { height: N, block_hash: 0xHASH })
VOTE_RE = re.compile(
    r'"event":"ReceiveVote","remote_peer":"([0-9a-f]{64})"'
)
PIVOT_RE = re.compile(
    r'pivot: Some\(PivotBlockDecision \{ height: (\d+), block_hash: 0x([0-9a-f]{64}) \}'
)

# Timestamp
TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)')

# Bootnode pattern from sniffer.toml
BOOTNODE_RE = re.compile(r'cfxnode://([0-9a-f]+)@([\d.]+):(\d+)')

# Message type names
MSG_NAMES = {
    "50": "PROPOSAL",
    "51": "VOTE",
    "52": "SYNC_INFO",
    "57": "CONSENSUS_MSG",
}


def load_pos_validators(filepath):
    """Load PoS validator NodeIds from file."""
    validators = {}
    if not filepath or not os.path.exists(filepath):
        return validators
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            # Format: NodeId  # power=N, proposals=N, votes=N
            parts = line.split("#")
            nid = parts[0].strip()
            if len(nid) == 64:
                validators[nid] = True
    return validators


def load_sniffer_records(filepath):
    """Load sniffer_records.jsonl."""
    records = {}
    if not filepath or not os.path.exists(filepath):
        return records
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


def parse_sniffer_toml():
    """Parse sniffer.toml to extract bootnode NodeIds and IPs."""
    bootnodes = {}
    toml_path = "/Users/xinghao/Desktop/conflux-rust/run/sniffer.toml"
    if not os.path.exists(toml_path):
        return bootnodes
    with open(toml_path) as f:
        for match in BOOTNODE_RE.finditer(f.read()):
            node_id = match.group(1)
            ip = match.group(2)
            bootnodes[node_id] = ip
    return bootnodes


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_possniffer_logs.py <log_file> [sniffer_records.jsonl] [pos_validators_nodeids.txt]")
        sys.exit(1)

    log_file = sys.argv[1]
    sniffer_records_path = sys.argv[2] if len(sys.argv) > 2 else None
    pos_validators_path = sys.argv[3] if len(sys.argv) > 3 else None

    print("=" * 80)
    print("[POSSNIFFER] Log Analysis - Block Source Tracing")
    print("=" * 80)

    # Load reference data
    pos_validators = load_pos_validators(pos_validators_path)
    sniffer_records = load_sniffer_records(sniffer_records_path)
    bootnodes = parse_sniffer_toml()

    print(f"\nReference data:")
    print(f"  PoS validators loaded: {len(pos_validators)}")
    print(f"  Sniffer records loaded: {len(sniffer_records)}")
    print(f"  Bootnodes loaded: {len(bootnodes)}")

    # Data structures
    peer_ip_map = {}           # NodeId → IP (current connections)
    peer_connect_events = []   # List of (ts, NodeId, IP)
    peer_disconnect_events = [] # List of (ts, NodeId)
    consensus_msg_events = []  # List of (ts, NodeId, IP, msg_type, account_addr)
    identity_bridges = []      # List of (ts, AccountAddress, NodeId, IP)
    account_to_nodeid = {}     # AccountAddress → NodeId (from identity bridge)
    account_to_ip = {}         # AccountAddress → IP (from identity bridge)
    proposals = []             # List of (ts, author_node_id, block_hash)
    votes_with_pivot = []      # List of (ts, author_node_id, pow_pivot_hash, pow_height)

    # Parse log file
    print(f"\nParsing log file: {log_file}")
    line_count = 0
    with open(log_file) as f:
        for line in f:
            line_count += 1
            if line_count % 100000 == 0:
                print(f"  Processed {line_count} lines...")

            ts = TS_RE.match(line)
            ts_str = ts.group(1) if ts else ""

            # [POSSNIFFER] events
            if "[POSSNIFFER]" in line:
                # Identity bridge (highest priority - critical mapping)
                m = IDENTITY_BRIDGE_RE.search(line)
                if m:
                    acct = m.group(1).lower()  # normalize to lowercase
                    nid = m.group(2).lower()
                    ip = m.group(3).strip()
                    identity_bridges.append((ts_str, acct, nid, ip))
                    account_to_nodeid[acct] = nid
                    account_to_ip[acct] = ip
                    peer_ip_map[nid] = ip
                    continue

                # Peer connected
                m = PEER_CONNECTED_RE.search(line)
                if m:
                    nid = m.group(1)
                    ip = m.group(2).strip()
                    peer_ip_map[nid] = ip
                    peer_connect_events.append((ts_str, nid, ip))
                    continue

                # Peer disconnected
                m = PEER_DISCONNECTED_RE.search(line)
                if m:
                    nid = m.group(1)
                    peer_ip_map.pop(nid, None)
                    peer_disconnect_events.append((ts_str, nid))
                    continue

                # Consensus message (now with account_addr)
                m = CONSENSUS_MSG_RE.search(line)
                if m:
                    nid = m.group(1).lower()
                    ip = m.group(2).strip()
                    msg_type = m.group(3)
                    acct = m.group(4)  # May be None if account_addr=None
                    if acct:
                        acct = acct.lower()
                    consensus_msg_events.append((ts_str, nid, ip, msg_type, acct))
                    # Also update account mapping if we got an account_addr
                    if acct:
                        account_to_nodeid[acct] = nid
                        account_to_ip[acct] = ip
                    continue

            # PoS consensus events (ReceiveProposal/ReceiveVote)
            if '"ReceiveProposal"' in line:
                m = PROPOSAL_RE.search(line)
                if m:
                    author = m.group(1)
                    block_hash = m.group(2)
                    proposals.append((ts_str, author, block_hash))

            elif '"ReceiveVote"' in line:
                m = VOTE_RE.search(line)
                pivot_m = PIVOT_RE.search(line)
                if m and pivot_m:
                    author = m.group(1)
                    pow_hash = pivot_m.group(2)
                    pow_height = int(pivot_m.group(1))
                    votes_with_pivot.append((ts_str, author, pow_hash, pow_height))

    print(f"\n  Total lines processed: {line_count}")
    print(f"  [POSSNIFFER] identity bridge events: {len(identity_bridges)}")
    print(f"  [POSSNIFFER] peer connect events: {len(peer_connect_events)}")
    print(f"  [POSSNIFFER] peer disconnect events: {len(peer_disconnect_events)}")
    print(f"  [POSSNIFFER] consensus message events: {len(consensus_msg_events)}")
    print(f"  ReceiveProposal events: {len(proposals)}")
    print(f"  ReceiveVote events with pivot: {len(votes_with_pivot)}")
    print(f"  Unique AccountAddress→NodeId mappings: {len(account_to_nodeid)}")

    # === Section 1: PoS Peer NodeId → IP Mapping ===
    print("\n" + "=" * 80)
    print("[1] PoS Peer NodeId → IP Mapping (from [POSSNIFFER] logs)")
    print("=" * 80)

    # Build a comprehensive mapping from all consensus messages
    all_consensus_ips = {}  # NodeId → set of IPs
    for ts, nid, ip, msg_type, acct in consensus_msg_events:
        if nid not in all_consensus_ips:
            all_consensus_ips[nid] = set()
        all_consensus_ips[nid].add(ip)

    # Also include peer connection events
    for ts, nid, ip in peer_connect_events:
        if nid not in all_consensus_ips:
            all_consensus_ips[nid] = set()
        all_consensus_ips[nid].add(ip)

    # Also include identity bridge events
    for ts, acct, nid, ip in identity_bridges:
        if nid not in all_consensus_ips:
            all_consensus_ips[nid] = set()
        all_consensus_ips[nid].add(ip)

    print(f"\n  Unique PoS peers with known IPs: {len(all_consensus_ips)}")
    print(f"\n  {'NodeId':<66} {'IP(s)':<30} {'Validator?':>10}")
    print(f"  {'-'*66} {'-'*30} {'-'*10}")
    for nid in sorted(all_consensus_ips.keys()):
        ips = ", ".join(sorted(all_consensus_ips[nid]))
        is_validator = "YES" if nid in pos_validators else ""
        print(f"  {nid} {ips:<30} {is_validator:>10}")

    # === Section 2: Identity Bridge Analysis (AccountAddress ↔ NodeId ↔ IP) ===
    print("\n" + "=" * 80)
    print("[2] Identity Bridge: AccountAddress ↔ NodeId ↔ IP")
    print("=" * 80)

    # Load known non-validator relay peers from pos_relay_peers.json
    relay_peers_path = "/Users/xinghao/Desktop/conflux-rust/pos_relay_peers.json"
    known_relay_peers = {}
    if os.path.exists(relay_peers_path):
        with open(relay_peers_path) as f:
            relay_data = json.load(f)
            for peer in relay_data.get("relay_peers", []):
                known_relay_peers[peer["account_address"]] = peer

    print(f"\n  Identity bridge events: {len(identity_bridges)}")
    print(f"  Unique AccountAddress → NodeId mappings: {len(account_to_nodeid)}")
    print(f"  Known relay peers from PoS analysis: {len(known_relay_peers)}")

    if identity_bridges:
        print(f"\n  {'AccountAddress':<66} {'NodeId':<66} {'IP':<22} {'Relay?':>6}")
        print(f"  {'-'*66} {'-'*66} {'-'*22} {'-'*6}")
        for ts, acct, nid, ip in identity_bridges:
            relay_info = ""
            if acct in known_relay_peers:
                rp = known_relay_peers[acct]
                relay_info = f"{'NV' if not rp.get('is_validator') else 'V'}({rp.get('proposal_count', 0)})"
            print(f"  {acct} {nid} {ip:<22} {relay_info:>6}")

    # Check which non-validator relay peers we've identified
    identified_nv_relays = []
    for acct, peer_info in known_relay_peers.items():
        if not peer_info.get("is_validator") and acct in account_to_nodeid:
            identified_nv_relays.append({
                "account_address": acct,
                "node_id": account_to_nodeid[acct],
                "ip": account_to_ip.get(acct, "unknown"),
                "proposal_count": peer_info.get("proposal_count", 0),
            })

    if identified_nv_relays:
        print(f"\n  *** Identified {len(identified_nv_relays)} non-validator relay peers! ***")
        for r in sorted(identified_nv_relays, key=lambda x: x["proposal_count"], reverse=True):
            print(f"    AccountAddr: {r['account_address'][:16]}...")
            print(f"    NodeId:      {r['node_id'][:16]}...")
            print(f"    IP:          {r['ip']}")
            print(f"    Proposals:   {r['proposal_count']}")
            # Suggest adding as trusted node
            print(f"    → Add to trusted_nodes: cfxnode://{r['node_id']}@{r['ip']}:32323")
            print()

    # === Section 3: PoS Validator IP Discovery ===
    print("\n" + "=" * 80)
    print("[3] PoS Validator IP Discovery")
    print("=" * 80)

    validator_ips = {}
    for nid, ips in all_consensus_ips.items():
        if nid in pos_validators:
            validator_ips[nid] = ips

    if validator_ips:
        print(f"\n  *** FOUND {len(validator_ips)} PoS validators with known IPs! ***")
        for nid, ips in sorted(validator_ips.items()):
            print(f"  NodeId: {nid}")
            print(f"  IP(s):  {', '.join(ips)}")
            print()
    else:
        print(f"\n  No PoS validators found in connected peers.")
        print(f"  Validators in PoS log: {len(pos_validators)}")
        print(f"  PoS peers connected: {len(all_consensus_ips)}")

    # === Section 4: Consensus Message Statistics by Sender ===
    print("\n" + "=" * 80)
    print("[4] Consensus Message Statistics by Sender")
    print("=" * 80)

    msg_by_peer = defaultdict(lambda: defaultdict(int))
    for ts, nid, ip, msg_type, acct in consensus_msg_events:
        msg_name = MSG_NAMES.get(msg_type, f"0x{msg_type}")
        msg_by_peer[nid][msg_name] += 1

    print(f"\n  {'NodeId':<66} {'IP':<20} {'PROPOSAL':>10} {'VOTE':>10} {'SYNC':>10} {'CONS':>10}")
    print(f"  {'-'*66} {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    for nid in sorted(msg_by_peer.keys(), key=lambda x: sum(msg_by_peer[x].values()), reverse=True):
        counts = msg_by_peer[nid]
        ip = list(all_consensus_ips.get(nid, {"?"}))[0]
        print(f"  {nid} {ip:<20} {counts.get('PROPOSAL',0):>10} {counts.get('VOTE',0):>10} {counts.get('SYNC_INFO',0):>10} {counts.get('CONSENSUS_MSG',0):>10}")

    # === Section 5: Cross-reference PoS proposals with sender IPs ===
    print("\n" + "=" * 80)
    print("[5] PoS Proposals → Author NodeId → Sender IP")
    print("=" * 80)

    # For each proposal, try to find the sender's IP
    # The author is the original proposer; the sender is the peer that relayed it to us
    proposal_count = Counter()
    author_to_ip = defaultdict(set)  # author NodeId → set of sender IPs

    for ts, author, block_hash in proposals:
        proposal_count[author] += 1
        # The author's IP might be in our mapping if they sent us messages directly
        if author in all_consensus_ips:
            author_to_ip[author].update(all_consensus_ips[author])

    print(f"\n  Total proposals: {len(proposals)}")
    print(f"  Unique proposers: {len(proposal_count)}")
    print(f"  Proposers with known IPs: {len(author_to_ip)}")

    if author_to_ip:
        print(f"\n  {'Author NodeId':<66} {'Proposals':>10} {'Known IP(s)':<30}")
        print(f"  {'-'*66} {'-'*10} {'-'*30}")
        for author in sorted(author_to_ip.keys(), key=lambda x: proposal_count[x], reverse=True):
            ips = ", ".join(sorted(author_to_ip[author]))
            is_val = " [VALIDATOR]" if author in pos_validators else ""
            print(f"  {author} {proposal_count[author]:>10} {ips:<30}{is_val}")

    # === Section 6: Cross-reference PoW pivot hashes with sniffer records ===
    print("\n" + "=" * 80)
    print("[6] PoW Block Source Tracing (PoS pivot hash ↔ sniffer records)")
    print("=" * 80)

    # Build pivot hash → author mapping
    pivot_to_authors = defaultdict(set)  # pow_hash → set of (author, ts)
    pivot_to_height = {}
    for ts, author, pow_hash, height in votes_with_pivot:
        pivot_to_authors[pow_hash].add((author, ts))
        pivot_to_height[pow_hash] = height

    print(f"\n  Unique PoW pivot hashes from PoS votes: {len(pivot_to_authors)}")
    print(f"  Sniffer PoW block records: {len(sniffer_records)}")

    matched = 0
    matched_records = []
    for pow_hash, authors in pivot_to_authors.items():
        if pow_hash in sniffer_records:
            matched += 1
            sr = sniffer_records[pow_hash]
            sniffer_ip = sr.get("first_peer_ip", "unknown")
            sniffer_nid = sr.get("first_peer_node_id", "unknown")
            sniffer_ts = sr.get("first_seen_at", "unknown")

            for author, pos_ts in authors:
                author_ips = ", ".join(sorted(all_consensus_ips.get(author, {"unknown"}))) if author in all_consensus_ips else "unknown"
                is_val = " [VALIDATOR]" if author in pos_validators else ""
                matched_records.append({
                    "pow_hash": pow_hash,
                    "height": pivot_to_height[pow_hash],
                    "pos_author": author,
                    "pos_author_ip": author_ips,
                    "pos_ts": pos_ts,
                    "sniffer_ip": sniffer_ip,
                    "sniffer_peer": sniffer_nid,
                    "sniffer_ts": sniffer_ts,
                    "is_validator": is_val,
                })

    print(f"  Matched blocks: {matched}")

    if matched_records:
        print(f"\n  {'PoW Hash':<20} {'Height':>10} {'PoS Author':>16} {'Author IP':>16} {'Sniffer IP':>16} {'Match?':>8}")
        print(f"  {'-'*20} {'-'*10} {'-'*16} {'-'*16} {'-'*16} {'-'*8}")
        for rec in sorted(matched_records, key=lambda x: x["height"])[:50]:
            author_short = rec["pos_author"][:8] + "..."
            author_ip = rec["pos_author_ip"].split(",")[0] if rec["pos_author_ip"] != "unknown" else "unknown"
            same_ip = "YES" if author_ip == rec["sniffer_ip"] else "no"
            print(f"  {rec['pow_hash'][:18]}... {rec['height']:>10} {author_short:>16} {author_ip:>16} {rec['sniffer_ip']:>16} {same_ip:>8}")

    # === Section 7: Summary and Recommendations ===
    print("\n" + "=" * 80)
    print("[7] Summary")
    print("=" * 80)
    print(f"""
  Identity bridge events: {len(identity_bridges)}
  AccountAddress → NodeId mappings: {len(account_to_nodeid)}
  Non-validator relay peers identified: {len(identified_nv_relays)}
  PoS peers discovered with IPs: {len(all_consensus_ips)}
  PoS validators with known IPs: {len(validator_ips)}
  PoW blocks matched: {matched}

  If validators' IPs were found, they can be added to sniffer.toml as
  bootnodes to ensure direct connection to block proposers.

  If non-validator relay peers were identified, add their
  cfxnode://NodeId@IP:32323 to trusted_nodes.json for persistent connections.

  If no validators were found, the sniffer may need to run longer or
  the PoS network may be using a separate overlay that requires
  specific configuration to join.
""")

    # Save results
    output_path = os.path.join(os.path.dirname(log_file), "possniffer_analysis.json")
    report = {
        "log_file": log_file,
        "total_lines": line_count,
        "identity_bridge_events": len(identity_bridges),
        "account_to_nodeid_mappings": len(account_to_nodeid),
        "pos_peers_with_ips": len(all_consensus_ips),
        "pos_validators_with_ips": len(validator_ips),
        "validator_ip_map": {k: list(v) for k, v in validator_ips.items()},
        "all_peer_ip_map": {k: list(v) for k, v in all_consensus_ips.items()},
        "identity_bridges": [
            {"account_addr": acct, "node_id": nid, "ip": ip}
            for ts, acct, nid, ip in identity_bridges
        ],
        "identified_non_validator_relays": identified_nv_relays,
        "matched_blocks": matched,
        "matched_details": matched_records[:100],
    }
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  Analysis report saved to: {output_path}")


if __name__ == "__main__":
    main()
