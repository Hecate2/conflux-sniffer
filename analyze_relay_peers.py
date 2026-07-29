#!/usr/bin/env python3
"""
Analyze non-validator relay peers from PoS log data.
These peers are not in the ValidatorSet but still relay PoS proposals.
They may be full nodes participating in PoS consensus messaging.
"""

import json
from collections import Counter, defaultdict

POS_LOG = "/Users/xinghao/Desktop/conflux-rust/pos-2026-07-26-02-51-27-795.log"
RELAY_PEERS_PATH = "/Users/xinghao/Desktop/conflux-rust/pos_relay_peers.json"
PROPOSAL_MAP_PATH = "/Users/xinghao/Desktop/conflux-rust/pos_proposal_to_proposer.jsonl"
VALIDATORS_PATH = "/Users/xinghao/Desktop/conflux-rust/pos_validators.json"
OUTPUT_PATH = "/Users/xinghao/Desktop/conflux-rust/non_validator_relay_analysis.json"

def main():
    # Load data
    with open(RELAY_PEERS_PATH) as f:
        relay_data = json.load(f)
    with open(VALIDATORS_PATH) as f:
        val_data = json.load(f)

    validator_addrs = {v['account_address'] for v in val_data['validators']}

    # Identify non-validator relay peers
    non_val_peers = [
        p for p in relay_data['relay_peers']
        if not p['is_validator']
    ]

    print("=" * 70)
    print("Non-Validator Relay Peer Analysis")
    print("=" * 70)
    print(f"\nTotal non-validator relay peers: {len(non_val_peers)}")
    print(f"Total proposals relayed by them: {sum(p['proposal_count'] for p in non_val_peers)}")

    # Load proposal details for timing analysis
    proposals_by_peer = defaultdict(list)
    with open(PROPOSAL_MAP_PATH) as f:
        for line in f:
            rec = json.loads(line)
            proposals_by_peer[rec['proposer_account_address']].append(rec)

    # Analyze each non-validator relay peer
    print(f"\n{'AccountAddress (first 16)':<20} {'Proposals':<12} {'Epochs':<10} {'Rounds Range':<20}")
    print(f"{'-'*20} {'-'*12} {'-'*10} {'-'*20}")

    analysis = []
    for peer in sorted(non_val_peers, key=lambda x: x['proposal_count'], reverse=True):
        addr = peer['account_address']
        props = proposals_by_peer.get(addr, [])

        epochs = set()
        rounds = []
        for p in props:
            epochs.add(p['epoch'])
            rounds.append(p['round'])

        round_range = f"{min(rounds)}-{max(rounds)}" if rounds else "N/A"

        print(f"{addr[:16]:<20} {peer['proposal_count']:<12} {len(epochs):<10} {round_range:<20}")

        # Check if this peer appears in any epoch's validator set
        # (they might have been a validator in an earlier epoch)
        appeared_in_epochs = sorted(epochs)

        analysis.append({
            'account_address': addr,
            'proposal_count': peer['proposal_count'],
            'epoch_count': len(epochs),
            'epochs_active': appeared_in_epochs[:5],  # first 5 epochs
            'round_range': round_range,
            'first_proposal_ts': props[0]['timestamp'] if props else None,
            'last_proposal_ts': props[-1]['timestamp'] if props else None,
        })

    # Cross-reference with sniffer IP distribution
    # The non-validator peers might be full nodes that we can see in sniffer records
    # We can't directly match AccountAddress to IP, but we can note which IPs
    # sent blocks to the sniffer and might be these peers

    print(f"\n{'=' * 70}")
    print("Cross-Reference: Non-validator relay peers vs Sniffer IP sources")
    print("=" * 70)
    print(f"\nNote: Direct matching is impossible without [POSSNIFFER] identity bridge.")
    print(f"The non-validator peers' AccountAddresses cannot be converted to P2P NodeIds.")
    print(f"However, they are already connected to our PoS node, so the modified sniffer")
    print(f"will automatically capture their NodeId↔IP mapping via the [POSSNIFFER] logs.")

    # Check if any non-validator relay peers were validators in earlier epochs
    # by looking at the full validator set history from the PoS log
    import re
    validator_history = {}  # epoch -> set of account_addresses
    val_entry_re = re.compile(r'([0-9a-f]{64}):\s*(\d+)')
    next_ep_re = re.compile(r'EpochState\s*\[epoch:\s*(\d+)')

    with open(POS_LOG) as f:
        for line in f:
            if 'ValidatorSet' not in line:
                continue
            next_ep_m = next_ep_re.search(line)
            if not next_ep_m:
                continue
            epoch = int(next_ep_m.group(1))
            validators = set()
            for m in val_entry_re.finditer(line):
                validators.add(m.group(1))
            validator_history[epoch] = validators

    print(f"\n{'=' * 70}")
    print("Validator History Check")
    print("=" * 70)
    print(f"Total epoch snapshots with validator sets: {len(validator_history)}")

    for peer in analysis:
        addr = peer['account_address']
        was_validator = []
        for epoch, vals in sorted(validator_history.items()):
            if addr in vals:
                was_validator.append(epoch)

        if was_validator:
            print(f"\n  {addr[:16]}... was a validator in epochs: {was_validator[:10]}")
            peer['was_validator_in_epochs'] = was_validator
        else:
            print(f"\n  {addr[:16]}... was NEVER a validator in any observed epoch")
            peer['was_validator_in_epochs'] = []

    # Save analysis
    output = {
        'summary': {
            'total_non_validator_relay_peers': len(non_val_peers),
            'total_proposals_by_non_validators': sum(p['proposal_count'] for p in non_val_peers),
            'percentage_of_all_proposals': round(
                sum(p['proposal_count'] for p in non_val_peers) /
                sum(p['proposal_count'] for p in relay_data['relay_peers']) * 100, 2
            ),
        },
        'non_validator_peers': analysis,
    }

    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n\nAnalysis saved to: {OUTPUT_PATH}")


if __name__ == '__main__':
    main()
