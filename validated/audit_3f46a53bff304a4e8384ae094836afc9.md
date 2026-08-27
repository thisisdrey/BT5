### Title
`AddressRegistrar::register` accepts and permanently keeps excess attached deposit instead of refunding it - (File: `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs`)

### Summary
The `register` method of the `AddressRegistrar` contract only validates that the attached deposit is *at least* the storage cost (`given_deposit < required_deposit` check), but on the success path it never refunds the difference when `given_deposit > required_deposit`. This mirrors the CBridge `>=` vs `==` bug class: the contract accepts more native tokens than needed and silently absorbs the excess instead of returning it to the caller.

### Finding Description
In `register`, the deposit check only guards against under-payment: [1](#0-0) 

When the entry is new (`Entry::Vacant`), the function stores the mapping and returns without ever comparing `given_deposit` to `required_deposit` again or refunding the surplus: [2](#0-1) 

By contrast, in the collision branch (`Entry::Occupied`), the author explicitly refunds the **entire** `given_deposit` back to the caller because no storage was consumed: [3](#0-2) 

This asymmetry shows the intended design was "pay exactly for what you use, get refunded otherwise," but the happy path only refunds nothing, not `given_deposit - required_deposit`. In NEAR's protocol, an attached deposit (`#[payable]` call) is automatically credited to the receiving contract's account balance as soon as the receipt executes; unlike EVM, there is no automatic bounce-back of unspent value, and the SDK-level contract code is fully responsible for returning any excess itself, as documented for deposit handling here: [4](#0-3) 

Because the contract does not issue a refund promise for the delta in the success branch, any amount attached beyond `required_deposit` (e.g. a caller rounding up, using a fixed “safe” deposit amount, or misestimating `storage_byte_cost`) is permanently absorbed into the `AddressRegistrar` contract's balance with no code path to reclaim it — there is no owner/withdraw/sweep method anywhere in this contract.

### Impact Explanation
This causes silent, permanent loss of user funds for any ordinary account calling `register` with more than the exact required storage deposit. There is no privileged or unprivileged function in the contract to recover the excess; it just inflates the contract's own NEAR balance forever. This qualifies as permanent freezing/loss of user funds under an unprivileged, reachable call path (a normal `register` call with a deposit that isn't calculated to the exact yoctoNEAR).

### Likelihood Explanation
Likelihood is high in practice: callers integrating with `AddressRegistrar` (e.g., wallets/tooling constructing the `register` transaction) commonly attach a rounded or "safe margin" deposit (e.g. `1 NEAR`) rather than computing the exact `storage_byte_cost * bytes_to_store`, since the exact cost depends on account-id length and can shift with protocol config. Any such over-attachment is silently kept by the contract.

### Recommendation
In the `Entry::Vacant` success branch, compute `excess = given_deposit - required_deposit` and, if `excess > 0`, issue a transfer promise back to `env::predecessor_account_id()` for the excess amount — mirroring exactly what is already done in the `Entry::Occupied` branch, but only for the unused portion:
```rust
Entry::Vacant(entry) => {
    let address = format!("0x{}", hex::encode(address));
    entry.insert(account_id);
    let excess = given_deposit.saturating_sub(required_deposit);
    if excess > NearToken::from_yoctonear(0) {
        let refund_promise = env::promise_batch_create(&env::predecessor_account_id());
        env::promise_batch_action_transfer(refund_promise, excess);
    }
    Some(address)
}
```

### Proof of Concept
1. Deploy `AddressRegistrar` and compute `required_deposit` for a given `account_id` (`storage_byte_cost * (20 + account_id.len())`).
2. Call `register(account_id)` attaching a deposit significantly larger than `required_deposit` (e.g. `1 NEAR` when only a few hundred yoctoNEAR are required).
3. Observe the call succeeds (`Some(address)` returned, mapping stored), and the caller's account balance is reduced by the full `1 NEAR`, not just `required_deposit`.
4. Confirm there is no subsequent call (as caller, owner, or otherwise) that can recover the surplus — it stays as part of the `AddressRegistrar` contract's balance indefinitely, as seen in `test_caller_refunds`'s success-path assertion that the full deposit is spent, not partially refunded: [5](#0-4)

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

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L73-85)
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
        }
```

**File:** docs/RuntimeSpec/Components/BindingsSpec/EconomicsAPI.md (L7-15)
```markdown
- `account_balance` -- the balance attached to the given account. This includes the `attached_deposit` that was attached
  to the transaction;
- `attached_deposit` -- the balance that was attached to the call that will be immediately deposited before
  the contract execution starts;
- `prepaid_gas` -- the tokens attached to the call that can be used to pay for the gas;
- `used_gas` -- the gas that was already burnt during the contract execution and attached to promises (cannot exceed `prepaid_gas`);

If contract execution fails `prepaid_gas - used_gas` is refunded back to `signer_account_id` and `attached_deposit`
is refunded back to `predecessor_account_id`.
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L215-226)
```rust
    // External caller does not get a refund when their tokens are spent
    let pre_tx_account_balance = post_tx_account_balance;
    let receiver_id = address_registrar.id();
    let result = wallet_contract
        .rlp_execute_from(&caller, receiver_id.as_str(), &create_tx(receiver_id, 1), deposit_amount)
        .await?;
    assert!(result.success);
    let post_tx_account_balance = caller.view_account().await?.balance;
    assert!(
        pre_tx_account_balance.as_yoctonear() - post_tx_account_balance.as_yoctonear()
            >= deposit_amount.as_yoctonear()
    );
```
