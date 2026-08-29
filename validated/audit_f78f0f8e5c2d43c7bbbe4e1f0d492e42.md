### Title
FunctionCall access key allowance cap can be exceeded via concurrent pending (uncertified) transactions - ([File: runtime/runtime/src/verifier.rs])

### Finding Description
`check_and_compute_new_allowance` computes the new allowance by subtracting a single transaction's `total_cost` from the allowance value read from the **committed** on-chain `AccessKey` state at verification time: [1](#0-0) 

This value is only persisted back to state later, when the resulting `AccessKeyUpdate` is applied (i.e. when the chunk/transaction is certified). Crucially, the balance-spending side of `verify_and_charge_tx_ephemeral` explicitly accounts for *other pending, not-yet-certified* transactions against the same account via `PendingConstraints.paid_from_balance`: [2](#0-1) 

but no equivalent accumulator exists for the FunctionCall key's `allowance`. `check_and_compute_new_allowance` is called with only `access_key` (the pre-commit, on-chain snapshot) and the current tx's `total_cost`, with no adjustment for allowance already "spent" by other pending/uncertified transactions signed with the same key: [3](#0-2) 

`PendingTransactionQueue`/`PendingAccount` in `chain/client/src/pending_transaction_queue.rs` tracks `access_key_tx_count`, `deploy_tx_count`, and `paid_from_balance` per account across uncertified chunks, but has no per-access-key allowance field: [4](#0-3) 

This asymmetry means that in the async/pending-chunk execution model (where multiple chunks can be built against the same pre-certification state before an earlier chunk's `AccessKeyUpdate` is applied — the very reason `PendingConstraints` exists), two (or more) transactions signed with the same FunctionCall access key, each individually costing ≤ allowance, can both pass `verify_and_charge_tx_ephemeral` against the same stale `allowance` snapshot. Their combined cost can exceed the allowance the account owner granted to that restricted key, even though the balance-level check (protected by `paid_from_balance`) correctly prevents insolvency of the signer's account.

### Impact Explanation
FunctionCall access keys are the mechanism NEAR uses to grant a lower-privileged, capped-spending key (e.g., an app/session key) restricted authority to spend from a full account's balance, with `allowance` being the explicit security boundary the account owner sets. This gap allows an attacker holding only such a restricted key to spend beyond the allowance ceiling that was authorized for it — an authorization/spending-limit escalation causing loss of funds from the signer's account beyond the owner-intended cap. This falls into the "authorization escalation" / unintended fund loss category, though the loss is bounded by the account's actual balance (the separate balance check via `paid_from_balance` still prevents insolvency).

### Likelihood Explanation
Exploitability depends entirely on the async/pending-chunk transaction-queue execution model (NEP-611-style) being reachable by an ordinary user: the attacker needs only to control a FunctionCall access key with a finite allowance and submit multiple transactions that land in different uncertified chunks before the first chunk's `AccessKeyUpdate` is applied. No validator/node privilege is required — this is purely a client-submitted-transaction race against normal chunk pipelining. Whether this window is practically reachable on mainnet depends on chunk/chain timing details of the pending-transaction-queue design that could not be fully confirmed from the available index (e.g., exact certification latency, whether `pending_gas_key_costs`-like tracking is applied elsewhere for allowance that wasn't visible in the retrieved code). This uncertainty should be resolved by inspecting the full `PendingConstraints` definition in `runtime/runtime/src/lib.rs` and the call sites in `chain/chain/src/runtime/mod.rs` and `chain/client/src/rpc_handler.rs` for any allowance-aware accounting not captured in the excerpts reviewed.

### Recommendation
Track per-(account, public_key) pending allowance consumption analogous to `PendingAccount.paid_from_balance`, and thread it into `verify_and_charge_tx_ephemeral`/`check_and_compute_new_allowance` so the effective allowance used for the check is `on_chain_allowance.saturating_sub(pending_allowance_spent)`, mirroring the existing balance-side protection.

### Proof of Concept
Unit test plan (to be run against `verify_and_charge_tx_ephemeral` / `check_and_compute_new_allowance`):
1. Create an `AccessKey` with `FunctionCallPermission { allowance: Some(X), .. }`.
2. Call `verify_and_charge_tx_ephemeral` twice with two different `Transaction`s (different nonces) each with `total_cost` = `0.6 * X`, both against the **same** unmodified `access_key`/`account` snapshot and a `PendingConstraints` whose `paid_from_balance` reflects only the account-balance side (not allowance).
3. Assert both calls return `TxVerdict::Success`/`Ok` (each individually ≤ X), and that the sum of accepted `total_cost` (1.2 * X) exceeds the original allowance `X`, demonstrating the cap bypass — while confirming the account balance check independently still passes because it only enforces conservation against the account's real balance, not the key's allowance ceiling.

### Citations

**File:** runtime/runtime/src/verifier.rs (L239-260)
```rust
fn check_and_compute_new_allowance(
    access_key: &AccessKey,
    account_id: &AccountId,
    public_key: &PublicKey,
    total_cost: Balance,
) -> Result<Option<Balance>, InvalidTxError> {
    let Some(fc) = access_key.permission.function_call_permission() else {
        return Ok(None);
    };
    let Some(allowance) = fc.allowance else {
        return Ok(None);
    };
    let new_allowance = allowance.checked_sub(total_cost).ok_or_else(|| {
        InvalidTxError::InvalidAccessKeyError(InvalidAccessKeyError::NotEnoughAllowance {
            account_id: account_id.clone(),
            public_key: public_key.clone().into(),
            allowance,
            cost: total_cost,
        })
    })?;
    Ok(Some(new_allowance))
}
```

**File:** runtime/runtime/src/verifier.rs (L307-317)
```rust
    // saturating_sub is fine here: on the consensus path pending constraints
    // are always default (zero), so the subtraction is exact. On the RPC /
    // chunk-production path it is best-effort and does not affect consensus.
    let available_balance = account.amount().saturating_sub(pending.paid_from_balance);
    if available_balance < total_cost {
        return TxVerdict::Failed(InvalidTxError::NotEnoughBalance {
            signer_id: account_id.clone(),
            balance: available_balance,
            cost: total_cost,
        });
    }
```

**File:** runtime/runtime/src/verifier.rs (L322-330)
```rust
    let new_allowance = match check_and_compute_new_allowance(
        access_key,
        account_id,
        tx.public_key(),
        total_cost,
    ) {
        Ok(a) => a,
        Err(e) => return TxVerdict::Failed(e),
    };
```

**File:** chain/client/src/pending_transaction_queue.rs (L129-137)
```rust
/// Aggregate for a set of transactions, per account.
/// Used both per-chunk and as pending transaction queue totals. Supports add/subtract.
#[derive(Clone, Default)]
struct PendingAccount {
    access_key_tx_count: usize,
    deploy_tx_count: usize,
    /// Access key total_cost + gas key deposit_cost.
    paid_from_balance: Balance,
}
```
