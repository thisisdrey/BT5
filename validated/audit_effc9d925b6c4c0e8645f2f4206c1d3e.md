### Title
Unbounded accounts-db nonce validation cost via repeated nonce-dedup gate bypass with a single fixed-priority forged nonce transaction - ([File: core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs])

### Summary
The nonce-dedup pre-filter in `TransactionViewReceiveAndBuffer::handle_packet_batch_message` only compares the incoming transaction's *declared* priority against the currently-queued nonce entry's priority, before any fee-payer solvency check occurs. Because the expensive `check_transaction_without_status_cache` accounts-db lookup (line 314) runs *before* `Consumer::check_fee_payer_unlocked` (line 333), an attacker can submit a transaction whose fee payer cannot actually pay, so the real (funded) nonce holder is never evicted, and its priority never changes — letting the attacker replay the same forged, unpayable, higher-priority-than-existing packet indefinitely, forcing a full bank nonce-validation lookup on every single replay for zero real fee.

### Finding Description
In `handle_packet_batch_message` (`core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs:292-360`), the flow is:

1. `drop_incoming_nonce_tx` (lines 301-305) is a cheap pre-filter: it drops the incoming packet only if `existing.priority >= priority || !container.is_queued(existing)`. This is meant to avoid paying for `check_transaction_without_status_cache` on strictly-dominated packets.
2. If the incoming declared `priority` is strictly greater than `existing.priority` for the targeted nonce address, the code proceeds to call `working_bank.check_transaction_without_status_cache` (line 314), which performs an accounts-db-backed nonce account lookup and blockhash-queue check.
3. Only *after* that expensive check succeeds does the code call `Consumer::check_fee_payer_unlocked` (line 333). If the fee payer is insolvent (e.g., zero-balance or attacker-controlled unfunded keypair), the transaction is dropped at line 338 **without ever reaching the nonce-map eviction logic at lines 348-356** — meaning `container.set_nonce_transaction_priority_id` is never called for this failed candidate, and the previously-queued, legitimate/funded nonce transaction's priority entry (`existing`) is left completely untouched.

Because `existing`'s priority never advances (it only advances when a `validated_nonce_address` transaction is actually inserted, which requires solvency), the attacker only needs to declare a priority once above the current `existing.priority`, and can then replay a functionally identical (or trivially perturbed, e.g. different padding/memo bytes to defeat the raw-byte sigverify `Deduper` in `perf/src/deduper.rs`) packet with that same fixed priority indefinitely. Each replay:
- Passes the cheap `drop_incoming_nonce_tx` gate (since `existing.priority` is frozen and `priority` stays greater).
- Forces a full `check_transaction_without_status_cache` accounts-db read of the nonce account and blockhash-queue on every single packet.
- Then fails `check_fee_payer_unlocked` and is discarded, at zero cost/fee to the attacker and with no state change that would ever stop the loop.

This inverts the intended invariant documented in the code's own comment ("prefilter without loading from accounts-db"): the prefilter is supposed to gate accounts-db work behind "will actually be admitted," but it only gates on priority ordering, not on solvency, and the expensive check is ordered before the solvency check. An unstaked, unprivileged remote client sending arbitrary QUIC/UDP packets to the leader's TPU can trigger this purely with packet content it fully controls (a durable-nonce-shaped transaction with an attacker-chosen compute-unit price and an unfunded/attacker fee payer), with no need for staked or leader access.

### Impact Explanation
This is a grossly underpriced pre-fee-validation CPU/accounts-db-load cost: a single cheap, never-paid-for packet can force unbounded repeated `check_transaction_without_status_cache` accounts-db lookups on the leader's banking stage, disproportionate to any fee actually collected (none, since the transaction is always rejected before fee charge/commit). This falls under the "disproportionate accounts-db lookups per fee paid" / QoS-evasion category, since the resource cost inflicted on `TransactionViewReceiveAndBuffer::handle_packet_batch_message` scales with attacker packet volume while circumventing the intended "don't do accounts-db work unless the packet could plausibly win the nonce slot" design.

### Likelihood Explanation
This requires only an unstaked network client capable of reaching the leader's TPU with the transaction bytes/QUIC streams it controls, no gossip/stake/config requirement. Constructing a syntactically valid nonce-shaped transaction (valid `AdvanceNonceAccount` instruction shape, arbitrary `ComputeBudget::SetComputeUnitPrice`) with an unfunded fee payer is trivial and fully within attacker control. Since the raw-packet `Deduper` in sigverify is a bloom filter over raw bytes, trivial per-packet byte variation (e.g., varying a memo, or trailing padding, or fee payer keypair used only for signing, not funding) defeats it cheaply while preserving semantics needed to hit the same nonce address and priority. This is realistically repeatable at high packet rates limited only by TPU-QUIC connection/stream throughput, not by any application-level defense in this code path.

### Recommendation
Reorder the checks so that a cheap-but-sufficient solvency/eligibility check (or at least a minimal fee-payer balance check) happens before the expensive `check_transaction_without_status_cache` call, or fold fee-payer-solvency verification into the pre-filter itself so packets that cannot possibly win the nonce slot (due to insolvency) are rejected before any accounts-db work is performed. Additionally, consider rate-limiting/tracking the number of `check_transaction_without_status_cache` invocations per unique nonce address or per fee payer within a time window as a backstop.

### Proof of Concept
```rust
// core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (test module)
//
// Flood a single nonce address with the SAME (fixed) forged priority using an
// unfunded fee payer, and confirm that check_transaction_without_status_cache
// (observed indirectly via ReceivingStats plus a counting hook around
// working_bank.check_transaction_without_status_cache) is invoked on every
// replay despite zero admitted/funded transactions ever changing state.

#[test]
fn test_nonce_dedup_forces_repeated_expensive_check_with_unfunded_fee_payer() {
    let (sender, receiver) = bounded(1024);
    let (bank_forks, mint_keypair) = test_bank_forks_with_fee();
    let (mut receive_and_buffer, mut container) =
        setup_transaction_view_receive_and_buffer(receiver, bank_forks.clone());
    let (nonce_pubkey, durable) = create_nonce_identity(&bank_forks, &mint_keypair.pubkey());

    // 1. Legitimate, funded nonce transaction is queued at LOW_FEE.
    send_transactions(
        &sender,
        &[create_nonce_transaction(&mint_keypair, &nonce_pubkey, LOW_FEE, durable)],
    );
    assert_eq!(receive(&mut receive_and_buffer, &mut container).num_buffered, 1);
    let baseline_entry = *container.get_nonce_transaction_priority_id(&nonce_pubkey).unwrap();

    // 2. Attacker keypair is UNFUNDED. Craft N nonce-shaped transactions each
    //    targeting the same nonce account with priority > baseline, varying
    //    trivial bytes (e.g. a memo/padding instruction) to bypass sigverify's
    //    raw-byte Deduper, but never funding the attacker fee payer.
    let attacker_kp = Keypair::new(); // zero balance
    const REPLAYS: usize = 1000;
    let mut total_num_dropped_on_fee_payer = 0;
    for i in 0..REPLAYS {
        let tx = create_nonce_transaction_with_memo(
            &attacker_kp, &nonce_pubkey, HIGH_FEE, durable, i as u64, // varying memo/nonce for uniqueness
        );
        send_transactions(&sender, &[tx]);
        let stats = receive(&mut receive_and_buffer, &mut container);
        // Each replay reaches check_transaction_without_status_cache (implied by
        // reaching the fee-payer check) but never evicts/replaces the real entry.
        total_num_dropped_on_fee_payer += stats.num_dropped_on_fee_payer;
        assert_eq!(stats.num_evicted_on_nonce_dedup, 0);
        assert_eq!(stats.num_buffered, 0);
    }

    // Assert: the cheap dedup pre-filter never dropped these (since priority > baseline
    // and baseline entry stays queued), i.e. all REPLAYS packets paid the accounts-db cost.
    assert_eq!(total_num_dropped_on_fee_payer, REPLAYS);

    // The legitimate funded transaction is still the queued nonce holder — unchanged
    // despite REPLAYS expensive validations having been forced by a single unfunded actor.
    assert_eq!(
        *container.get_nonce_transaction_priority_id(&nonce_pubkey).unwrap(),
        baseline_entry
    );
}
```
Expected assertion outcome: `total_num_dropped_on_fee_payer == REPLAYS`, demonstrating `REPLAYS` full `check_transaction_without_status_cache` accounts-db lookups were performed for **zero** admitted/funded transactions, versus the single legitimately-funded transaction that was actually buffered — i.e., disproportionate verification work forced by a single unpaid attacker identity.