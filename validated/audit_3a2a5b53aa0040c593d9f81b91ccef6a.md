[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** runtime/runtime/src/verifier.rs (L363-369)
```rust
/// Verify a gas key transaction and compute the charge outcome.
///
/// This function performs validation only and does NOT mutate `account` or `access_key`.
/// Callers are responsible for applying state changes based on the returned variant:
/// - `Success(result)`: apply all state changes via `result.apply()`.
/// - `DepositFailed { result, .. }`: apply gas-only changes via `result.apply()`.
/// - `Failed(_)`: no state changes.
```

**File:** runtime/runtime/src/verifier.rs (L415-419)
```rust
    let tx_nonce = tx.nonce().nonce();
    let effective_nonce = std::cmp::max(current_nonce, pending.max_nonce);
    if let Err(e) = verify_nonce(tx_nonce, effective_nonce, block_height, tx.nonce_mode()) {
        return TxVerdict::Failed(e);
    }
```

**File:** runtime/runtime/src/verifier.rs (L485-490)
```rust
    // Check account has enough balance for deposits, accounting for
    // pending balance costs from prior txs. saturating_sub is fine: on the
    // consensus path pending constraints are always default (zero), so the
    // subtraction is exact. On the RPC / chunk-production path it is
    // best-effort.
    let available_balance = account.amount().saturating_sub(pending.paid_from_balance);
```
