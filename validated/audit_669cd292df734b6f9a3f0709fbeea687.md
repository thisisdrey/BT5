## Title
Inconsistent gas-price scaling between pending-transaction-queue gas-key cost estimation and Spice chunk-apply execution price causes gas-key transfer DoS - (`chain/client/src/pending_transaction_queue.rs`, `chain/chain/src/spice/chunk_application.rs`)

## Summary
This is the nearcore analog of the reported Cod3x/Aave bug class: two code paths compute the "same" derived quantity from two *differently scaled* inputs, and one of the paths assumes (via a hard, non-saturating subtraction) that the two will always agree. In the Solidity report, `scaledTotalSupply` (scaled by the minipool index) is subtracted from a balance scaled by the main-pool index. In nearcore, the `PendingTransactionQueue` estimates a gas key's committed cost using the **previous block's** `next_gas_price`, while Spice's actual chunk-apply path prices the same transaction using the **chunk's own block's** `next_gas_price` — a different value whenever gas price is adjusting. `verify_and_charge_gas_key_tx_ephemeral` then performs an un-guarded `checked_sub` on the assumption these always match, causing legitimate gas-key transfers to be wrongly rejected.

## Finding Description
The standard nearcore convention for "the gas price used to execute the chunks contained in block B" is the gas price stored in **B's parent header** (`prev_block_header.next_gas_price()`), as implemented in the canonical helper: [1](#0-0) 

The `PendingTransactionQueue` follows exactly this convention when estimating pending costs for gas-key transactions included in an uncertified chunk of block `block_hash`: [2](#0-1) 

That estimate (`tx_cost(config, tx, gas_price)`, using `gas_cost`) is aggregated into `pending_gas_key_costs`/`paid_from_gas_key`: [3](#0-2) 

However, under Spice, the *actual* execution price used when the chunk is later certified/applied is built by `build_spice_apply_chunk_block_context`, which uses `block_header.next_gas_price()` — the price field on the **same** block that contains the chunk, not its parent — with an explicit TODO acknowledging this is a placeholder: [4](#0-3) 

This is a scaling mismatch structurally identical to the reported bug: `prev(B).next_gas_price()` (used by PTQ to size the "committed" gas-key cost) and `B.next_gas_price()` (used to actually burn/deduct at certification) are two different quantities whenever the gas-price adjustment formula moves the price (i.e., whenever chunk utilization isn't exactly 50%, per the documented gas-price formula): [5](#0-4) 

`verify_and_charge_gas_key_tx_ephemeral` then treats the PTQ-derived `pending.paid_from_gas_key` as an exact, never-exceeded quantity relative to the real on-chain `gas_key_info.balance`, using a hard `checked_sub` (not `saturating_sub`, unlike the sibling account-balance check a few lines above which explicitly uses `saturating_sub` "because pending constraints ... is best-effort"): [6](#0-5) 

The comment directly preceding this code documents the (violated) assumption:

> "Unlike account balance, gas key balance only changes through transactions that PTQ explicitly tracks, so pending should never exceed the balance."

Because the estimate and the real debit are computed from different `next_gas_price` scalars, this invariant does not hold in general.

## Impact Explanation
When gas price rises between the chunk's parent block and the chunk's own block (i.e., `B.next_gas_price() > prev(B).next_gas_price()`), the actual on-chain debit against the gas key at certification is larger than what PTQ estimated when admitting subsequent transactions. This means `available_gas_key_balance = gas_key_info.balance - pending.paid_from_gas_key` reported by PTQ is stale/optimistic relative to the eventual real balance, so a transaction that PTQ admits as fitting the gas key's balance can end up unable to be paid for once prior uncertified chunks are actually applied at the higher real price — the account's legitimate gas-key transfers get stuck/rejected (`NotEnoughGasKeyBalance`) even though, from the user's perspective, the funds should have sufficed. Conversely, when gas price falls, PTQ's `paid_from_gas_key` over-estimates the real cost, so it can spuriously reject transactions that would actually have succeeded (`checked_sub` in the `else` branch immediately returns `InvalidTxError::NotEnoughGasKeyBalance`), denying service to a paying, unprivileged signer's normal transfers/meta-transactions using a gas key — the requested bug class (transfer DoS caused by inconsistent unit/scale accounting between two code paths).

## Likelihood Explanation
This triggers whenever: (1) the chain is running under the `Spice` protocol feature (chunk production/certification is decoupled across blocks so a chunk can sit "pending" while gas price moves), (2) a user has multiple gas-key transactions in flight across the uncertified window, and (3) chunk utilization moves gas price away from 50% between the parent block used for PTQ estimation and the chunk's own block used at certification — an ordinary, expected occurrence of network load, not an attacker-controlled or malicious-node condition. It requires no privileged signer and is reachable purely by an ordinary account submitting normal gas-key transactions during normal load fluctuation.

## Recommendation
Make `PendingTransactionQueue::add_chunk_transactions` (and `reinitialize_pending_transaction_queue`) price gas-key transactions using the exact same gas price that Spice's `build_spice_apply_chunk_block_context` will use at certification (`block_header.next_gas_price()` of the chunk's own block, once that TODO is resolved to price by execution results), or vice versa — the two price sources must be unified. Additionally, replace the hard `checked_sub` in `verify_and_charge_gas_key_tx_ephemeral`'s gas-key balance check with the same defensive `saturating_sub` pattern already used for `paid_from_balance`, so a residual mismatch degrades gracefully (best-effort admission control) instead of relying on an invariant that the two pricing paths do not actually guarantee.

## Proof of Concept
1. Enable the `protocol_feature_spice` build; fund a gas key with a balance sized to cover exactly N transfers at the current gas price (as in `test_ptq_gas_key_balance_enforcement`, `test-loop-tests/src/tests/pending_transaction_queue.rs:437-491`).
2. Drive chunk utilization for the parent block of the chunk containing the gas-key tx to <50%, so `prev(B).next_gas_price()` used by `add_chunk_transactions`/PTQ is lower than `B.next_gas_price()` which `build_spice_apply_chunk_block_context` will use at certification.
3. Submit gas-key transactions until PTQ's `pending.paid_from_gas_key` (computed at the lower price) indicates the balance still covers one more transaction, and admit it via `verify_and_charge_gas_key_tx_ephemeral`.
4. When the chunk is actually applied/certified at the higher `B.next_gas_price()`, the real debit against `gas_key_info.balance` (via the tx-cost/verifier path) exceeds what PTQ reserved, so the balance is insufficient at execution — while symmetrically, running the scenario with falling gas price shows PTQ rejecting an admissible transaction with `InvalidTxError::NotEnoughGasKeyBalance` purely due to the stale price used for the pending estimate.

### Citations

**File:** chain/chain/src/chain.rs (L3029-3038)
```rust
        // Before `FixApplyChunks` feature, gas price was taken from current
        // block by mistake. Preserve it for backwards compatibility.
        let gas_price = if is_new_chunk {
            prev_block_header.next_gas_price()
        } else {
            // TODO(#10584): next_gas_price should be Some() if derived from
            // Block and None if derived from OptimisticBlock. Attempt to take
            // next_gas_price since OptimisticBlock enabled must fail.
            block_header.next_gas_price()
        };
```

**File:** chain/client/src/client.rs (L696-720)
```rust
        for block_hash in &uncertified_block_hashes {
            let block = chain.get_block(block_hash)?;
            let epoch_id = epoch_manager.get_epoch_id(block_hash)?;
            let protocol_version = epoch_manager.get_epoch_protocol_version(&epoch_id)?;
            let config = runtime_adapter.get_runtime_config(protocol_version);
            let prev_header = chain.get_block_header(block.header().prev_hash())?;
            let gas_price = prev_header.next_gas_price();

            for chunk_header in block.chunks().iter_new() {
                let shard_id = chunk_header.shard_id();
                let shard_uid = shard_id_to_uid(epoch_manager, shard_id, &epoch_id)?;
                if !shard_tracker
                    .cares_about_shard_this_or_next_epoch(block.header().prev_hash(), shard_id)
                {
                    continue;
                }
                let chunk = chain.get_chunk(&chunk_header.chunk_hash())?;
                let transactions = chunk.to_transactions();
                pending_transaction_queue.lock().get_or_create(shard_uid).add_chunk_transactions(
                    *block_hash,
                    transactions,
                    &config,
                    gas_price,
                );
            }
```

**File:** chain/client/src/pending_transaction_queue.rs (L270-307)
```rust
            // Track gas key costs (gas_key_cost for gas key txs).
            if is_gas_key_tx {
                let gas_key_entry = chunk_data
                    .gas_key_costs
                    .entry((signer_id.clone(), public_key.clone()))
                    .or_insert(Balance::ZERO);
                *gas_key_entry = gas_key_entry.saturating_add(cost.gas_cost);
            }

            // Scan actions for WithdrawFromGasKey (affects gas key balance).
            for action in tx.actions() {
                if let Action::WithdrawFromGasKey(withdraw) = action {
                    let gas_key_entry = chunk_data
                        .gas_key_costs
                        .entry((signer_id.clone(), withdraw.public_key.clone()))
                        .or_insert(Balance::ZERO);
                    *gas_key_entry = gas_key_entry.saturating_add(withdraw.amount);
                }
            }

            // Track nonces. Done last to consume signer_id and public_key.
            let max_nonce =
                chunk_data.nonces.entry((signer_id, public_key, nonce_index)).or_insert(0);
            *max_nonce = std::cmp::max(*max_nonce, nonce);
        }

        // Merge chunk data into pending transaction queue totals.
        for (account_id, chunk_account) in &chunk_data.accounts {
            let total_account = self.pending_accounts.entry(account_id.clone()).or_default();
            total_account.add(chunk_account);
        }
        for (nonce_key, &chunk_nonce) in &chunk_data.nonces {
            self.pending_nonces.entry(nonce_key.clone()).or_default().add(chunk_nonce);
        }
        for (gas_key, &chunk_gas_key_cost) in &chunk_data.gas_key_costs {
            let entry = self.pending_gas_key_costs.entry(gas_key.clone()).or_insert(Balance::ZERO);
            *entry = entry.saturating_add(chunk_gas_key_cost);
        }
```

**File:** chain/chain/src/spice/chunk_application.rs (L219-237)
```rust
pub fn build_spice_apply_chunk_block_context(
    block_header: &BlockHeader,
    prev_block_execution_results: &BlockExecutionResults,
    epoch_manager: &dyn EpochManagerAdapter,
) -> Result<ApplyChunkBlockContext, Error> {
    // TODO(spice): gas price should be based on execution results and not part of the
    // block since it's calculated based on gas usage during execution.
    let gas_price = block_header.next_gas_price();
    let congestion_info =
        build_block_congestion_info(block_header, prev_block_execution_results, epoch_manager)?;
    let bandwidth_requests =
        build_block_bandwidth_requests(block_header, prev_block_execution_results, epoch_manager)?;
    Ok(ApplyChunkBlockContext::from_header(
        block_header,
        gas_price,
        congestion_info,
        bandwidth_requests,
    ))
}
```

**File:** protocol-model/spec/economics.md (L43-49)
```markdown
### 4. Gas-price adjustment
The next block's gas price is a load-feedback controller (`core/primitives/src/block.rs:440` — `compute_next_gas_price_checked`):
- Formula (`block.rs:417`): `next_gas_price = gas_price * (1 + (gas_used/gas_limit − 1/2) * adjustment_rate)`. Implemented as the exact integer ratio `numerator/denominator` at `block.rs:460`. When utilization is exactly 50% the price is unchanged; above 50% it rises, below it falls.
- If the block was skipped (`gas_limit == 0`) the price is unchanged (`block.rs:449`).
- The result is clamped to `[min_gas_price, max_gas_price]` (`block.rs:471`).
- `min_gas_price` is `MIN_GAS_PRICE_NEP_92_FIX` (`100_000_000` yN) for chains whose genesis is `PROD_GENESIS_PROTOCOL_VERSION`, else the genesis value (`chain/chain/src/types.rs:199`, const at `core/primitives/src/version.rs:29`). `max_gas_price` is `min(genesis_max_gas_price, min_gas_price * 20)` (`types.rs:207`, `MAX_GAS_MULTIPLIER = 20` at `types.rs:191`).
- The chain applies this over one block via `compute_next_gas_price_checked` (called at `block.rs:182`); under Spice it folds over certified results (`compute_gas_price_from_certified_results_checked`, `block.rs:479`).
```

**File:** runtime/runtime/src/verifier.rs (L421-446)
```rust
    // Check gas key has enough balance for gas costs, accounting for
    // pending gas key costs (prior gas key txs + pending WithdrawFromGasKey).
    // Unlike account balance, gas key balance only changes through transactions
    // that PTQ explicitly tracks, so pending should never exceed the balance.
    let Some(available_gas_key_balance) =
        gas_key_info.balance.checked_sub(pending.paid_from_gas_key)
    else {
        tracing::error!(
            target: "runtime",
            balance = %gas_key_info.balance,
            paid_from_gas_key = %pending.paid_from_gas_key,
            "pending gas key costs exceed gas key balance"
        );
        return TxVerdict::Failed(InvalidTxError::NotEnoughGasKeyBalance {
            signer_id: account_id.clone(),
            balance: Balance::ZERO,
            cost: gas_cost,
        });
    };
    if available_gas_key_balance < gas_cost {
        return TxVerdict::Failed(InvalidTxError::NotEnoughGasKeyBalance {
            signer_id: account_id.clone(),
            balance: available_gas_key_balance,
            cost: gas_cost,
        });
    }
```
