I have enough to conclude. The `AddressRegistrar::register` function in `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs` is a direct analog: it validates `given_deposit >= required_deposit` (line 54: `if given_deposit < required_deposit { panic }`), but on the success path (`Entry::Vacant`, lines 66-72) it never refunds the excess `given_deposit - required_deposit` — the whole attached deposit is captured by the contract's balance permanently, unlike the collision path (`Entry::Occupied`, lines 73-84) which explicitly refunds the *entire* `given_deposit`. This mirrors the reported Solidity bug class of accepting `>=` as valid without returning the excess.

### Title
Excessive attached deposit permanently lost when registering an address in `AddressRegistrar::register` - (File: runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs)

### Summary
The `AddressRegistrar` contract, part of the ETH-implicit wallet-contract tooling, accepts any attached deposit greater than or equal to the computed storage cost as "sufficient" for the `register` method. Unlike the collision path, the success path never refunds the difference between the deposit given and the deposit actually required, permanently trapping the excess in the contract.

### Finding Description
In `register` [1](#0-0) , `required_deposit` is computed as a function of the fixed 20-byte address plus the length of `account_id`, and the check only rejects deposits that are strictly less than `required_deposit`:
```
if given_deposit < required_deposit { env::panic_str(...); }
```
Any deposit `>= required_deposit` passes. When the entry is vacant (the common, successful registration case), the code inserts the mapping and returns the address [2](#0-1)  without ever computing or refunding `given_deposit - required_deposit`. Contrast this with the collision branch, which explicitly refunds the caller's *entire* `given_deposit` via `promise_batch_action_transfer` [3](#0-2) , proving the contract author was aware refunding is the correct behavior in at least one path, yet omitted it for the success path.

Since `#[payable]` NEAR functions accept any attached deposit and the deposit simply becomes part of the contract's own account balance once accepted (it is not automatically returned by the runtime), any yoctoNEAR sent beyond the exact storage requirement is retained by the `AddressRegistrar` account forever, with no method exposed to reclaim or withdraw it.

### Impact Explanation
Any user (or wallet/tooling) that over-estimates the required storage deposit — e.g. due to client-side estimation drift, rounding, or simply attaching a round number like 1 NEAR "to be safe" — will have the excess balance permanently locked in the `AddressRegistrar` contract, which is a fixed, non-owner-controlled contract with no withdrawal method. This is a permanent, irreversible loss of funds for the calling account, consistent with the reported bug class ("funds lost forever").

### Likelihood Explanation
Likelihood is low-to-moderate: it requires a caller-side over-estimation of the exact deposit needed (which depends on `account_id.len()` and the current `storage_byte_cost`), similar to the referenced report's "requires a mistake from the user." Given `register` is a `#[payable]` method intended to be called by ordinary, unprivileged users/wallets registering ETH-implicit account mappings, this is a realistically reachable path.

### Recommendation
Refund the excess over `required_deposit` on the success path, mirroring the collision-path behavior:
```rust
Entry::Vacant(entry) => {
    entry.insert(account_id);
    if given_deposit > required_deposit {
        let excess = NearToken::from_yoctonear(given_deposit.as_yoctonear() - required_deposit.as_yoctonear());
        let refund_promise = env::promise_batch_create(&env::predecessor_account_id());
        env::promise_batch_action_transfer(refund_promise, excess);
    }
    ...
}
```
Alternatively, require `given_deposit == required_deposit` exactly and panic otherwise.

### Proof of Concept
1. Compute `required_deposit` for a given `account_id` (bytes = `20 + account_id.len()`, times `storage_byte_cost`).
2. Call `register(account_id)` attaching `required_deposit + X` yoctoNEAR for some `X > 0`, where the derived address for `account_id` is not already registered.
3. Observe: the call succeeds, the mapping is inserted, and the caller's account balance decreases by `required_deposit + X`, but the contract issues no refund receipt — `X` is permanently absorbed into the `AddressRegistrar` contract's balance with no way for the caller (or anyone) to retrieve it, as confirmed by the existing test `test_register_without_deposit` [4](#0-3)  which only checks that *some* deposit ≥ the fixed amount is taken, not that exact/excess accounting is enforced.

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L249-296)
```rust
/// Test asserting the address registrar requires a deposit.
#[tokio::test]
async fn test_register_without_deposit() -> anyhow::Result<()> {
    let TestContext { worker, address_registrar, .. } = TestContext::new().await?;

    let method = "register";
    let args = br#"{"account_id": "birchmd.near"}"#;
    let result = address_registrar.call(method).args(args.to_vec()).transact().await?;
    assert!(result.is_failure(), "Call without deposit must fail");

    let pre_tx_account_balance = address_registrar.as_account().view_account().await?.balance;
    let deposit_amount = NearToken::from_yoctonear(320000000000000000000);
    let result = worker
        .root_account()?
        .call(address_registrar.id(), method)
        .args(args.to_vec())
        .deposit(deposit_amount)
        .transact()
        .await?;

    let output: Option<String> = result.json()?;
    assert_eq!(output.as_deref(), Some("0x4bfcff9a964925adf801c866f6ada98bd7ec40ca"));
    let post_tx_account_balance = address_registrar.as_account().view_account().await?.balance;
    assert!(
        post_tx_account_balance.as_yoctonear() - pre_tx_account_balance.as_yoctonear()
            >= deposit_amount.as_yoctonear()
    );

    // Sending a duplicate transaction does not take the deposit again.
    let pre_tx_account_balance = post_tx_account_balance;
    let result = worker
        .root_account()?
        .call(address_registrar.id(), method)
        .args(args.to_vec())
        .deposit(deposit_amount)
        .transact()
        .await?;

    let output: Option<String> = result.json()?;
    assert_eq!(output, None);
    let post_tx_account_balance = address_registrar.as_account().view_account().await?.balance;
    assert!(
        post_tx_account_balance.as_yoctonear() - pre_tx_account_balance.as_yoctonear()
            < deposit_amount.as_yoctonear()
    );

    Ok(())
}
```
