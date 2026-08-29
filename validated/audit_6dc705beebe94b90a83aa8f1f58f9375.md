### Title
Gas-refund receipt to a self-deleted account panics via `action_implicit_account_creation_transfer`'s `panic!("must be implicit")` — shard-halting DoS - ([File: runtime/runtime/src/lib.rs], [File: runtime/runtime/src/actions.rs])

### Summary
When a gas-key-funded receipt's gas refund (`Receipt::new_gas_refund`) targets an account that was deleted (via `DeleteAccountAction`) before the refund is applied, `action_transfer_or_implicit_account_creation` looks up `account: None` and, because `is_refund` is `true`, only a `debug_assert!(!is_refund)` (a no-op in release builds) guards this branch before it unconditionally calls `action_implicit_account_creation_transfer` on the (named, non-implicit) `receiver_id`. That function panics with `panic!("must be implicit")` for `AccountType::NamedAccount`, crashing the node applying the chunk.

### Finding Description
`try_refund_gas_key_balance` (`runtime/runtime/src/actions.rs:115-132`) is invoked from `action_transfer_or_implicit_account_creation` (`runtime/runtime/src/lib.rs:2910-2958`) only when `account` is `Some`: [1](#0-0) 

If `account` is `None` — i.e., the receiver account has been deleted — execution falls into the `else` branch: [2](#0-1) 

`debug_assert!(!is_refund)` only fires in debug builds; in a release/production build it is compiled out, so the code proceeds to call `action_implicit_account_creation_transfer` with `receipt.receiver_id()` — which, for a gas refund, is set to the original named account (`Receipt::new_gas_refund` sets `receiver_id` and `signer_id` to the same original account, `core/primitives/src/receipt.rs:518-536`). Inside `action_implicit_account_creation_transfer` (`runtime/runtime/src/actions.rs:213-272`), the account type is matched, and for a regular `NamedAccount` (any account that isn't implicit), the code unconditionally panics: [3](#0-2) 

Exploit flow: an ordinary account `alice.near` issues a receipt attaching gas to one of its gas keys (a `FunctionCall`/promise funded via a gas key with an access key balance). This generates a corresponding gas-refund receipt (`new_gas_refund`) targeting `alice.near` as both `signer_id` and `receiver_id`. Before this refund receipt is applied (refunds are generated as outgoing local receipts and typically processed in a subsequent chunk/block for the same shard), `alice.near` submits a `DeleteAccountAction` deleting itself (`action_delete_account`). When the refund receipt is later processed, `get_account` returns `None` for `alice.near`, `is_refund` is `true`, and the release-mode code path proceeds into `action_implicit_account_creation_transfer`, which panics because `alice.near` is a `NamedAccount`, not an implicit account.

This is fully attacker-reachable using only an ordinary account's own transactions (fund a gas key, trigger a receipt that spends less gas than attached, then self-delete before the refund lands) — no privileged access is required, and no existing signature/nonce/access-key/gas-metering check prevents self-deletion mid-flight or blocks generation/delivery of the refund to the now-deleted account.

### Impact Explanation
A panic during chunk application (inside `apply_action`/receipt processing) causes the node processing that shard's chunk to crash. Since all honest validators/chunk producers deterministically execute the same state transition, this is a reproducible shard-halting panic reachable by an unprivileged account performing ordinary actions (self-funding a gas key, spending it, then deleting its own account before the refund arrives) — matching the "shard-halting panic" bounty category.

### Likelihood Explanation
Preconditions: (1) create an account, (2) create/fund a gas key with an access-key balance, (3) issue at least one receipt that attaches gas from that key such that a nonzero gas refund is generated, (4) submit `DeleteAccount` for the same account before that refund receipt is processed (feasible because refunds are asynchronous, generated as outgoing local receipts and applied in a following chunk, giving a natural window). This requires no elevated privileges, no validator/node access, and is fully repeatable — cost is only the standard gas/storage cost of a few ordinary transactions from a self-funded account. This makes it a low-cost, highly repeatable denial-of-service.

### Recommendation
Replace the `debug_assert!(!is_refund)` in `action_transfer_or_implicit_account_creation`'s `None`-account branch with an explicit, safe runtime handling: when `account` is `None` and `is_refund` is `true` (or when `receipt.receiver_id()` is not implicit and cannot be created via transfer), treat the refund as unrecoverable and record it as a burnt/lost-balance system event (e.g., emit a balance-burnt log / metric) instead of falling through to `action_implicit_account_creation_transfer`. Additionally, `action_implicit_account_creation_transfer` should not be reachable at all for non-implicit `NamedAccount` ids in production; guard the call site with an explicit `account_is_implicit`/`account_id.get_account_type()` check rather than relying on an internal `panic!` as a correctness backstop.

### Proof of Concept
Integration test plan (in `runtime/runtime/src/tests/apply.rs`, alongside existing `new_gas_refund` tests):
1. Create account `alice.near` with a gas key that has a nonzero balance/allowance.
2. Submit a transaction from `alice.near` using the gas key to call a contract method that consumes less gas than attached (guaranteeing a nonzero gas refund) — apply this chunk so the outgoing gas-refund receipt (`Receipt::new_gas_refund` targeting `alice.near`) is generated but not yet applied (it should land as an incoming/local receipt for the next chunk).
3. In the same or next chunk (before the refund receipt is processed), submit and apply a `DeleteAccount` action for `alice.near`.
4. Apply the chunk that processes the pending gas-refund receipt.
5. Assert: in a release-configured test (or by removing/ignoring `debug_assert`), the apply call panics (or, once fixed, assert that `alice.near` remains absent from state, no implicit account is spuriously created, and the refunded balance is explicitly recorded/logged as burnt rather than silently misallocated to an unrelated implicit-account-creation transfer).

### Citations

**File:** runtime/runtime/src/lib.rs (L2921-2934)
```rust
    Ok(if let Some(account) = account.as_mut() {
        let is_gas_refund = is_refund && action_receipt.signer_id() == receipt.receiver_id();
        // For gas refunds, try to refund to the gas key first. If the signer key is a gas key,
        // the refund goes to the gas key balance and we skip crediting the account balance.
        if is_gas_refund
            && try_refund_gas_key_balance(
                state_update,
                receipt.receiver_id(),
                &action_receipt.signer_public_key(),
                deposit,
            )?
        {
            return Ok(());
        }
```

**File:** runtime/runtime/src/lib.rs (L2944-2957)
```rust
    } else {
        debug_assert!(!is_refund);
        action_implicit_account_creation_transfer(
            state_update,
            &apply_state,
            &apply_state.config.fees,
            account,
            actor_id,
            receipt.receiver_id(),
            deposit,
            apply_state.block_height,
            epoch_info_provider,
        );
    })
```

**File:** runtime/runtime/src/actions.rs (L268-271)
```rust
        // This panic is unreachable as this is an implicit account creation transfer.
        // `check_account_existence` would fail because `account_is_implicit` would return false for a Named account.
        AccountType::NamedAccount => panic!("must be implicit"),
    }
```
