### Title
Excess attached deposit is permanently locked in `AddressRegistrar::register` on successful registration - (File: `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs`)

### Summary
The `AddressRegistrar::register` method, part of the eth-implicit wallet infrastructure, is a `#[payable]` function that only validates that `attached_deposit >= required_deposit`. When a caller overpays and the registration succeeds (the `Entry::Vacant` branch), the excess deposit is never refunded, unlike the collision branch which explicitly refunds the entire attached deposit. The extra tokens remain stuck in the contract's balance permanently.

### Finding Description
`register` computes the exact storage cost needed to persist the new `address -> account_id` mapping and only checks a lower bound: [1](#0-0) 

If `given_deposit` exceeds `required_deposit` and no address collision occurs, execution proceeds into the `Entry::Vacant` arm, which inserts the entry and returns the address string — with no refund logic of any kind: [2](#0-1) 

Compare this to the `Entry::Occupied` (collision) branch, which explicitly schedules a `promise_batch_action_transfer` to return `given_deposit` in full to the predecessor: [3](#0-2) 

This is the exact analog of the reported bug class: a payable entry point checks `msg.value`/`attached_deposit` with a `>=` (not exact-match) comparison, consumes only the required portion, and silently retains the surplus with no refund path in the success case.

### Impact Explanation
Any ordinary user calling `register` (directly or via the wallet-contract's `rlp_execute` cross-contract call flow that funds this registrar, as exercised in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs`) who attaches more than the minimal required storage deposit — which is trivial to do accidentally since predicting the exact per-byte storage cost requires reproducing the contract's internal byte-count formula — permanently loses the difference. There is no owner/admin withdraw mechanism shown in this contract, so the surplus is unrecoverable: a permanent loss of funds for the caller, matching the "permanent freezing of funds" acceptance criterion.

### Likelihood Explanation
High likelihood of accidental triggering: callers must independently compute `20 + account_id.len()` bytes times the current `storage_byte_cost()` to attach the exact amount; any small overestimation (e.g. rounding up for safety, as is common practice) triggers permanent loss. No malicious actor or privileged role is required — this is reachable directly by any ordinary signer/predecessor account.

### Recommendation
In the `Entry::Vacant` success branch, compute the actual bytes used for storage (mirroring the collision branch) and refund `given_deposit.checked_sub(required_deposit)` (or the precisely measured storage cost) back to `env::predecessor_account_id()` via a transfer promise, exactly as already done in the `Entry::Occupied` branch. Alternatively, change the check at line 54 to require an exact match and reject overpayment, or use `env::storage_usage()`-based measurement of actual usage delta after insertion to compute a precise refund.

### Proof of Concept
1. Call `AddressRegistrar::register(account_id)` with `attached_deposit = required_deposit + X` yoctoNEAR, where `required_deposit = storage_byte_cost() * (20 + account_id.len())`.
2. Since `given_deposit >= required_deposit`, the guard at lines 54–61 passes.
3. `self.addresses.entry(address)` is `Vacant` (first-time registration), so the code path at lines 66–72 executes: the entry is inserted and `Some(address)` returned — no transfer promise is created.
4. The registrar contract's balance permanently retains `X` yoctoNEAR that were part of the caller's `attached_deposit`; the caller has no way to reclaim it, verifiable by comparing predecessor balance before and after the call as done in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs::test_caller_refunds`, which only asserts refund behavior on failure/collision, not on the successful-overpayment path.

### Citations

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L48-61)
```rust
        // Must store the address and the account id
        let bytes_to_store = 20 + (account_id.len() as u128);
        let required_deposit =
            NearToken::from_yoctonear(env::storage_byte_cost().as_yoctonear() * bytes_to_store);
        let given_deposit = env::attached_deposit();
        // The caller must pay for the storage cost of registering.
        if given_deposit < required_deposit {
            let message = format!(
                "Insufficient deposit to cover storage cost. Given={} Expected={}",
                given_deposit.as_yoctonear(),
                required_deposit.as_yoctonear(),
            );
            env::panic_str(&message);
        }
```

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L65-72)
```rust
        match self.addresses.entry(address) {
            Entry::Vacant(entry) => {
                let address = format!("0x{}", hex::encode(address));
                let log_message = format!("Added entry {} -> {}", address, account_id);
                entry.insert(account_id);
                env::log_str(&log_message);
                Some(address)
            }
```

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L73-84)
```rust
            Entry::Occupied(entry) => {
                let log_message = format!(
                    "Address collision between {} and {}. Keeping the former.",
                    entry.get(),
                    account_id
                );
                env::log_str(&log_message);
                // Transfer the deposit back to the caller since no storage was updated.
                let refund_promise = env::promise_batch_create(&env::predecessor_account_id());
                env::promise_batch_action_transfer(refund_promise, given_deposit);
                None
            }
```
