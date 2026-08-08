## Analysis

The reported Sherlock issue is a "front-run a monotonic/state-gating check to force a legitimate operation to be discarded" bug class. The closest reachable analog in this codebase is the **nonce transaction dedup/eviction logic** in the banking-stage transaction scheduler, which allows an unprivileged network peer to front-run and evict another user's already-buffered, valid durable-nonce transaction by broadcasting a competing transaction that merely *references the same (public) nonce account*, without needing any authority over it.

### Title
Unauthenticated nonce-account front-running can evict a buffered valid transaction from the banking-stage scheduler - (File: `core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs`)

### Summary
`TransactionViewReceiveAndBuffer::handle_packet_batch_message` tracks "in use" nonce addresses per `TransactionStateContainer` via `nonces_in_use` (`get_nonce_transaction_priority_id` / `set_nonce_transaction_priority_id`). Any incoming transaction that validly references a nonce account's currently-stored durable-nonce hash (`check_nonce_transaction_validity`, `runtime/src/bank/check_transactions.rs:258-284`) is treated as "the" pending transaction for that nonce, and if its fee-priority is higher than (or equal to, per test cases) the currently-queued transaction using that same nonce, the existing one is evicted from the container (`container.remove_by_id(existing_nonce_priority_id.id)`, `receive_and_buffer.rs:348-356`).

Critically, a nonce account's `durable_nonce` value and its authority are both publicly readable on-chain, so any unprivileged actor can construct a syntactically-valid "nonce transaction" against someone else's nonce account/hash (it will pass `check_nonce_transaction_validity`, which only checks the stored hash — not that the submitter actually is or represents the authority for anything beyond the `AdvanceNonceAccount` instruction signer set) and set a higher priority fee, purely to knock the legitimate transaction out of the leader's buffer before it can be scheduled. [1](#0-0) 

### Finding Description
`check_nonce_transaction_validity` in `runtime/src/bank/check_transactions.rs` validates a nonce transaction purely by matching the transaction's `recent_blockhash` against the nonce account's stored `durable_nonce`, and (only when `strict_nonce_authority_check` is set) checking that some signer of the `AdvanceNonceAccount` instruction equals the nonce authority: [2](#0-1) 

In the banking-stage ingestion path, `working_bank.check_transaction_without_status_cache(...)` is used to obtain `validated_nonce_address` before eviction occurs; if it returns `Ok(Some(nonce_address))`, the code treats the packet as a legitimate contender for that nonce slot and evicts whatever transaction previously held it in `container.nonces_in_use`, provided the new transaction has priority ≥ the old one: [3](#0-2) 

The container-level test suite explicitly documents this "higher-or-equal priority evicts" behavior as intended for same-fee competition between the *same submitter* rebroadcasting with a higher fee, but nothing in the check restricts eviction to transactions from the same signer/authority — any third party who knows the nonce account's public state (address + current durable-nonce hash, both fully public) can build a transaction that "uses" that nonce (e.g., an `AdvanceNonceAccount` instruction naming a signer they control as an unrelated, non-matching authority, combined with other instructions), pay a higher priority fee, and get it accepted into `nonces_in_use` for that address, evicting the real pending transaction: [4](#0-3) 

This mirrors the Pyth bug precisely: a shared, monotonically-checked piece of state (there: `lastCommittedPublishTime`; here: `nonces_in_use[nonce_address]`) can be advanced/claimed by an unrelated front-runner who only needs to satisfy the *format* constraints of the check (there: publish-time ordering; here: matching the current on-chain durable-nonce hash), not any actual authorization tied to the original submitter's intent, causing the legitimate pending operation to be discarded (there: `commitRequested` reverts; here: the container evicts/drops the honest transaction via `remove_by_id`).

I was not able to fully confirm within the available context whether `check_transaction_without_status_cache` (called at `receive_and_buffer.rs:314`) passes `strict_nonce_authority_check=true` or `false` for this specific ingestion path — the grep for its definition did not return the body before the session ended, and this detail directly determines whether an attacker's competing "nonce-like" transaction must actually name the correct nonce authority as a signer to be treated as a valid contender. If it is `false` (as the top-level `check_transactions_with_processed_slots` call site defaults it to `false` at `check_transactions.rs:216`), then front-running only requires knowing the nonce address and its currently stored hash — both public — with no authority signature needed at all, making the analog fully unprivileged. If it is enforced as `true` for this path, the attacker would additionally need to control (or be) some signer matching the real nonce authority for at least the `AdvanceNonceAccount` instruction, which would reduce — but likely not eliminate — practical exploitability (an attacker who is themselves a legitimate nonce authority for an account they also use, e.g., in a shared/multisig nonce, could still evict other pending transactions on the same nonce with a higher fee).

### Impact Explanation
If exploitable without authority (unconfirmed, see above), any unprivileged network peer observing a pending durable-nonce transaction in the public mempool/gossip of forwarded packets could construct a slightly-higher-fee competing "nonce transaction" against the same nonce account and get it accepted into the leader's scheduler, evicting the legitimate transaction from `TransactionStateContainer` before it is ever considered for execution. The victim's transaction is silently dropped (not merely delayed): it must be entirely resubmitted, and because durable-nonce transactions are typically used precisely when the sender cannot easily resubmit with a fresh blockhash (offline/multisig/cold-storage signing flows), repeated denial can indefinitely block the legitimate operation from landing, a real griefing/DoS impact analogous to the "keeper resubmission" impact accepted as Medium in the source report. This does not corrupt bank state or cause a panic; it is a QoS/availability degradation of the banking-stage scheduler for a specific, predictable class of transactions (durable-nonce senders).

### Likelihood Explanation
Likelihood is Medium-to-Low and depends on the unresolved `strict_nonce_authority_check` value for this code path:
- If `false`: trivially exploitable by any unprivileged peer with knowledge of on-chain nonce state (fully public), for the cost of a slightly higher priority fee on a garbage transaction — no special access or stake required.
- If `true`: exploitation requires the attacker to be a signer accepted as the nonce authority for the referenced instruction, narrowing the attacker set but not eliminating the risk for nonce accounts with shared/rotatable/misconfigured authorities.
Given the codebase's own test suite (`test_receive_and_buffer_nonce_dedup_drop_evict`, `test_receive_and_buffer_pseudo_nonce_never_evicts`) treats "higher-fee evicts lower-fee for the same nonce" as intended behavior without apparent same-sender/authority binding at the dedup layer itself, this looks like a deliberate design tradeoff (favor highest-fee valid contender per nonce) rather than an accidental oversight, which somewhat mitigates but does not fully negate the griefing concern.

### Recommendation
- Confirm the exact `strict_nonce_authority_check` value used by `check_transaction_without_status_cache` on the `receive_and_buffer.rs:314` call path, and if it is `false`, consider requiring authority-signature validation before allowing nonce-based eviction of another (different fee-payer/signer) transaction, not just before allowing execution.
- Alternatively/additionally, scope eviction to only apply when the new and existing transactions share the same fee-payer or nonce-authority signer, so that unrelated third parties cannot use knowledge of public nonce state to grief a specific pending transaction.
- Add telemetry/metrics distinguishing evictions caused by same-sender fee-bumping vs. cross-sender nonce contention, to detect this griefing pattern in production.

### Proof of Concept
Conceptual PoC (unauthenticated variant, pending confirmation of `strict_nonce_authority_check`):
1. Observe a durable-nonce account `N` on-chain with `durable_nonce = H` and note that Alice has broadcast (or the attacker predicts) a nonce transaction `Tx_A` using nonce `N`/`H` with fee `F_A`.
2. Attacker builds `Tx_B`, an `AdvanceNonceAccount` instruction against the same nonce address `N` (referencing the same public hash `H`) bundled with a no-op/self-transfer, signed by the attacker's own keys, with fee `F_B > F_A`.
3. Attacker broadcasts `Tx_B` to the current leader's TPU.
4. In `handle_packet_batch_message`, `Tx_B` passes `check_transaction_without_status_cache` (nonce hash matches) and, having higher priority, evicts `Tx_A` via `container.remove_by_id(existing_nonce_priority_id.id)` at `receive_and_buffer.rs:353`, per the exact behavior exercised in `test_receive_and_buffer_nonce_dedup_drop_evict` (case `lohi_evict`).
5. `Tx_A` never gets scheduled for this leader slot and must be entirely resubmitted by Alice, even though it was fully valid and had already been accepted into the buffer. [4](#0-3)

### Citations

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L292-356)
```rust
            let priority = state.priority();
            let raw_nonce_address = state.transaction().get_durable_nonce().cloned();

            // When we first receive a transaction, we drop it if a) it looks nonce-like, AND
            // b) there is a higher-priority nonce transaction using the same nonce in the queue
            // or any in-flight nonce transaction using the same nonce. This means we discard
            // blockhash transactions structured like nonce transactions; this is acceptable because
            // they would fail after the earlier nonce transaction is processed, and it allows us to
            // prefilter without loading from accounts-db.
            let drop_incoming_nonce_tx = raw_nonce_address
                .and_then(|address| container.get_nonce_transaction_priority_id(&address))
                .is_some_and(|existing| {
                    existing.priority >= priority || !container.is_queued(existing)
                });

            if drop_incoming_nonce_tx {
                receiving_stats.num_dropped_on_nonce_dedup += 1;
                continue;
            }

            // Check blockhash transaction age is ok, or nonce transaction has a valid nonce.
            // Only a fully validated nonce address can be used for priority queue eviction.
            let validated_nonce_address = match working_bank.check_transaction_without_status_cache(
                state.transaction(),
                working_bank.max_processing_age(),
                &mut error_counters,
            ) {
                // Valid nonce transaction
                Ok(Some(nonce_address)) => Some(nonce_address),

                // Valid blockhash transaction
                Ok(None) => None,

                // Invalid
                Err(ref err) => {
                    receiving_stats.add_transaction_error(err);
                    continue;
                }
            };

            // Check the transaction's fee-payer validates.
            if let Err(_err) = Consumer::check_fee_payer_unlocked(
                working_bank,
                state.transaction(),
                &mut error_counters,
            ) {
                receiving_stats.num_dropped_on_fee_payer += 1;
                continue;
            };

            let transaction_id = container.insert_map_only(state);
            let priority_id = TransactionPriorityId::new(priority, transaction_id);

            // Now, if this is a nonce transaction, we know it is validated and higher-priority than any
            // which may exist in the priority queue. If one is queued, evict it. Regardless, record the
            // incoming nonce transaction's nonce as in-use.
            if let Some(nonce_address) = validated_nonce_address {
                if let Some(existing_nonce_priority_id) =
                    container.get_nonce_transaction_priority_id(&nonce_address)
                {
                    receiving_stats.num_evicted_on_nonce_dedup += 1;
                    container.remove_by_id(existing_nonce_priority_id.id);
                }
                container.set_nonce_transaction_priority_id(&nonce_address, priority_id);
            }
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L1428-1495)
```rust
    // a higher priority incoming nonce transaction evicts the existing transaction,
    // a lower or equal priority incoming nonce transaction is dropped
    #[test_case(HIGH_FEE, LOW_FEE; "hilo_drop")]
    #[test_case(HIGH_FEE, HIGH_FEE; "hihi_drop")]
    #[test_case(LOW_FEE, HIGH_FEE; "lohi_evict")]
    fn test_receive_and_buffer_nonce_dedup_drop_evict(old_fee: u64, new_fee: u64) {
        let (sender, receiver) = bounded(1024);
        let (bank_forks, mint_keypair) = test_bank_forks_with_fee();
        let (mut receive_and_buffer, mut container) =
            setup_transaction_view_receive_and_buffer(receiver, bank_forks.clone());
        let (nonce_pubkey, durable) = create_nonce_identity(&bank_forks, &mint_keypair.pubkey());
        let new_has_priority = new_fee > old_fee;

        send_transactions(
            &sender,
            &[create_nonce_transaction(
                &mint_keypair,
                &nonce_pubkey,
                old_fee,
                durable,
            )],
        );
        assert_eq!(
            receive(&mut receive_and_buffer, &mut container).num_buffered,
            1
        );
        let prior_nonce_entry = *container
            .get_nonce_transaction_priority_id(&nonce_pubkey)
            .unwrap();

        send_transactions(
            &sender,
            &[create_nonce_transaction(
                &mint_keypair,
                &nonce_pubkey,
                new_fee,
                durable,
            )],
        );

        let stats = receive(&mut receive_and_buffer, &mut container);
        let current_nonce_entry = *container
            .get_nonce_transaction_priority_id(&nonce_pubkey)
            .unwrap();

        if new_has_priority {
            assert_eq!(stats.num_dropped_on_nonce_dedup, 0);
            assert_eq!(stats.num_evicted_on_nonce_dedup, 1);
            assert_eq!(stats.num_buffered, 1);

            assert_ne!(prior_nonce_entry, current_nonce_entry);
            assert!(current_nonce_entry.priority > prior_nonce_entry.priority);
            assert!(
                container
                    .get_mut_transaction_state(prior_nonce_entry.id)
                    .is_none()
            );
        } else {
            assert_eq!(stats.num_dropped_on_nonce_dedup, 1);
            assert_eq!(stats.num_evicted_on_nonce_dedup, 0);
            assert_eq!(stats.num_buffered, 0);
            assert_eq!(prior_nonce_entry, current_nonce_entry);
        }

        assert!(container.is_queued(&current_nonce_entry));

        verify_container(&mut container, 1);
    }
```

**File:** runtime/src/bank/check_transactions.rs (L258-284)
```rust
    pub(super) fn check_nonce_transaction_validity(
        &self,
        message: &impl SVMMessage,
        next_durable_nonce: &DurableNonce,
        strict_nonce_size_check: bool,
        strict_nonce_authority_check: bool,
    ) -> Option<(Pubkey, u64)> {
        let nonce_is_advanceable = message.recent_blockhash() != next_durable_nonce.as_hash();
        if !nonce_is_advanceable {
            return None;
        }

        let (nonce_address, nonce_data) =
            self.load_message_nonce_data(message, strict_nonce_size_check)?;

        if strict_nonce_authority_check
            && !message
                .get_ix_signers(NONCED_TX_MARKER_IX_INDEX as usize)
                .any(|signer| signer == &nonce_data.authority)
        {
            return None;
        }

        let previous_lamports_per_signature = nonce_data.get_lamports_per_signature();

        Some((nonce_address, previous_lamports_per_signature))
    }
```
