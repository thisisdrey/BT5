### Title
Function-call access key `allowance` is not tracked across pending (uncertified) transactions, allowing a restricted key to spend beyond its configured limit - (File: `runtime/runtime/src/verifier.rs`, `chain/client/src/pending_transaction_queue.rs`)

### Summary
The reported TapiocaOFT bug is a "stale allowance re-use" issue: a spend-limiting allowance is checked against a value that is not actually decremented before it can be reused by a subsequent call, letting the same allowance authorize far more spending than intended. The nearcore analog is the `FunctionCallPermission.allowance` on a restricted access key, which is meant to cap how much value a key can move (independent of the account's total balance), but the pending-transaction bookkeeping used to admit multiple not-yet-certified transactions per chunk protects the account *balance* but not the key's *allowance*.

### Finding Description
`verify_and_charge_tx_ephemeral` computes the transaction's validity without mutating trie state; it is designed to be re-run for every uncertified/pending transaction and to be consistent with concurrently pending transactions that have not yet been applied/certified [1](#0-0) .

To protect the account balance against being oversubscribed by multiple pending, not-yet-applied transactions, it explicitly subtracts `pending.paid_from_balance` from the account's on-trie balance before comparing against `total_cost`: [2](#0-1) 

However, immediately after, the function-call key's `allowance` check is performed using the raw, on-trie `access_key.permission` value with no equivalent adjustment for pending/uncertified spends against the same key: [3](#0-2) [4](#0-3) 

The `PendingConstraints`/`PendingAccount` structures that back `pending.paid_from_balance` only aggregate `access_key_tx_count`, `deploy_tx_count`, and `paid_from_balance` (which mirrors the account amount reservation) — there is no field tracking allowance-already-consumed-but-not-yet-committed for a specific access key: [5](#0-4) 

Because `access_key.permission.allowance` is only mutated in the trie once a transaction is actually applied (via `VerificationResult::apply`, called during certification, not during the ephemeral admission checks used for pending/uncertified chunks) [6](#0-5) , multiple transactions signed with the same restricted `FunctionCallPermission` key — each submitted into different uncertified chunks before any of them is certified — will each independently read the *same* stale `allowance` value from the trie and each pass `check_and_compute_new_allowance` as long as its own individual cost is under that limit, even though the sum of their costs exceeds the key's configured allowance. This mirrors exactly the TapiocaOFT root cause: the balance-limiting counter (`allowance`) is checked against a value that isn't atomically decremented relative to other in-flight spends drawing on the same permission, so the same allowance quota can be consumed multiple times before it is ever written back.

### Impact Explanation
`FunctionCallPermission.allowance` exists specifically so that an account owner can hand out a restricted key with a hard spending cap for gas/fees, independent of and much smaller than the account's total balance (e.g., a "trip management" key capped at 3000 tokens as documented) [7](#0-6) . If the allowance check does not account for other pending, uncertified transactions signed by the same restricted key, an attacker who compromises or is handed such a key can pipeline multiple transactions across uncertified chunks and drain the owner's account balance well beyond the allowance the owner intended to authorize — an authorization/spend-limit escalation directly on the victim's account funds.

### Likelihood Explanation
This requires the "Spice" pending-transaction-queue / uncertified-chunk pipelining mechanism to admit more than one transaction from the same restricted access key before any of them is certified and applied to the trie — a scenario the pending queue is explicitly built to support (it exists to allow multiple not-yet-certified transactions from the same account across chunks). Any ordinary holder of a restricted `FunctionCall` access key (or an attacker who obtains one) could attempt this by simply submitting several transactions in quick succession.

### Recommendation
Extend `PendingAccount`/`PendingConstraints` to also track, per `(account_id, public_key)`, the cumulative `allowance` already committed by uncertified pending transactions (analogous to `paid_from_balance`), and subtract that reserved amount from `fc.allowance` inside `check_and_compute_new_allowance` before validating a new transaction, exactly as is already done for `account.amount()` via `pending.paid_from_balance`.

### Proof of Concept
Conceptual PoC (exact chunk/pending-queue harness not fully traced in this review):
1. Owner creates a `FunctionCall` access key with `allowance = 100`.
2. Attacker (or key holder) submits transaction A with `total_cost = 90` into uncertified chunk 1, and transaction B with `total_cost = 90` into uncertified chunk 2, before chunk 1 is certified.
3. Both `verify_and_charge_tx_ephemeral` calls read the same on-trie `allowance = 100` (since neither has been applied/certified yet) and each independently pass `check_and_compute_new_allowance` (90 ≤ 100), because `pending.paid_from_balance` protects `account.amount()` but not `fc.allowance`.
4. Once both chunks are certified, 180 has been drawn against a key whose allowance was meant to cap spending at 100.

Note: I was not able to fully trace every step of how `PendingConstraints` is constructed/passed from `chain/client/src/pending_transaction_queue.rs` into `verify_and_charge_tx_ephemeral` end-to-end (e.g., in `chain/chain/src/runtime/mod.rs`), so I cannot rule out an allowance-specific safeguard existing elsewhere that was not surfaced by search. If such a safeguard exists, this finding would not hold, and a Devin session with full filesystem access should confirm by tracing `PendingConstraints` construction and consumption end-to-end.

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

**File:** runtime/runtime/src/verifier.rs (L262-269)
```rust
/// Verify a regular (non-gas-key) transaction and compute the charge outcome.
///
/// Returns `TxVerdict::Success` or `TxVerdict::Failed` (never `DepositFailed`).
/// Callers should apply state changes via `VerificationResult::apply` on success.
///
/// This function performs no mutation; all state changes are returned in the
/// `VerificationResult`.
pub fn verify_and_charge_tx_ephemeral(
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

**File:** runtime/runtime/src/lib.rs (L312-323)
```rust
impl VerificationResult {
    /// Apply the state changes described by this result to the given account and access key.
    pub fn apply(&self, account: &mut Account, access_key: &mut AccessKey) {
        account.set_amount(self.new_account_amount);
        match &self.access_key_update {
            AccessKeyUpdate::Regular { nonce, new_allowance } => {
                access_key.nonce = *nonce;
                if let Some(a) = new_allowance {
                    access_key.permission.function_call_permission_mut().unwrap().allowance =
                        Some(*a);
                }
            }
```

**File:** docs/RuntimeSpec/Scenarios/CrossContractCall.md (L51-64)
```markdown
```
FunctionCallPermission {
    allowance: Some(3000),
    receiver_id: "travel_agency",
    method_names: [ "reserve_trip", "cancel_trip" ]
}
```

This scenario might arise when someone Alice's parent has given them a restricted access to `alice_near` account by
creating an access key that can be used strictly for trip management.
This access key allows up to `3000` tokens to be spent (which includes token transfers and payments for gas), it can
be only used to call `travel_agency` and it can be only used with the `reserve_trip` and `cancel_trip` methods.
The way runtime treats this case is almost exactly the same as the previous one, with the only difference on how it verifies
the signature of on the signed transaction, and that it also checks for allowance to not be exceeded.
```
