This confirms `AddressRegistrar` is a deployed part of the eth-implicit wallet infrastructure — it is the registrar contract used alongside the Wallet Contract (`runtime/near-wallet-contract/implementation/wallet-contract`) so that eth-implicit-derived accounts can be looked up by their named `AccountId`, deployed and initialized via `deploy_address_registrar` in `test_context.rs`. Any ordinary NEAR user (or the wallet-contract deployment tooling) calls `register()` on this contract with an attached deposit.

### Title
Permanent freezing of overpaid deposit in `AddressRegistrar::register` (eth-implicit wallet infrastructure) - (File: `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs`)

### Summary
`AddressRegistrar::register` requires `attached_deposit >= required_deposit` to cover the storage cost of the new `address -> account_id` mapping, but on the success path it only checks a lower bound and never refunds the difference when the caller overpays. Unlike the reported LiquidityMiningManager analog (which reverts unless the *exact* amount is sent), this NEAR-side contract has the opposite but related consequence of the same bug class: excess `msg.value`/`attached_deposit` is never returned, permanently trapping the caller's extra NEAR in the contract with no owner, no withdraw method, and no other way to reclaim it.

### Finding Description
In `register()` ( [1](#0-0) ), the function computes `required_deposit` from `storage_byte_cost` and the size of the entry to be stored, then panics only if `given_deposit < required_deposit`. When `given_deposit > required_deposit` and the address is not already registered (`Entry::Vacant` branch, lines 65-72), the contract inserts the mapping and returns the address string, but never issues a promise transfer to refund the excess deposit — contrast this with the `Entry::Occupied` branch a few lines below (lines 73-85) which explicitly creates a refund promise (`env::promise_batch_action_transfer(refund_promise, given_deposit)`) for the *entire* deposit when the call doesn't need to store anything.

The contract exposes no `withdraw`, no owner-only sweep function, and `new()` (lines 25-28) sets no privileged account that could later retrieve stuck balance. Because `#[payable]` methods accept any deposit amount and the struct has no mechanism to move NEAR back out except the one refund path in the collision branch, any yoctoNEAR sent above the exact storage requirement on a successful registration is permanently locked in the contract's balance.

### Impact Explanation
This is a permanent freezing of user funds: NEAR tokens attached in excess of the storage requirement by any caller of `register()` become unrecoverable, since the contract account has no withdrawal capability and is not controlled by an admin key that could sweep the balance out through a generic `FunctionCall`/`Transfer` action (the account is deployed with only the four public methods `new`, `register`, `lookup`, `get_address`). Since `register` is a normal, permissionless entry point intended to be called by regular users interacting with the eth-implicit wallet ecosystem, and users have no reliable way to know the exact yoctoNEAR-precise storage cost ahead of time (it depends on `env::storage_byte_cost()` and the exact byte length of the `AccountId` being registered), overpayment is a realistic, easy-to-trigger outcome, not a contrived edge case.

### Likelihood Explanation
Likelihood is high for accidental loss: any caller who rounds up their attached deposit (a common practice to avoid the `NotEnoughBalanceForDeposit`-style panic seen at line 60) or who computes the byte cost slightly incorrectly for a longer `account_id` will overpay and receive no refund, unlike almost all comparable NEAR-native flows that carefully refund excess deposit (e.g. `action_deterministic_state_init` at `runtime/runtime/src/deterministic_account_id.rs:57-91`, which returns any deposit above the exact `missing_amount` needed for storage staking).

### Recommendation
In the `Entry::Vacant` success branch of `register()`, compute `excess = given_deposit - required_deposit` and, if `excess > 0`, issue a promise transfer refunding it to `env::predecessor_account_id()`, mirroring the refund logic already present in the `Entry::Occupied` branch just below it.

### Proof of Concept
1. Deploy `AddressRegistrar` and call `new()`.
2. Compute `required_deposit` for a target `account_id` (e.g., `storage_byte_cost * (20 + account_id.len())`).
3. Call `register(account_id)` attaching `required_deposit + 1_000_000` yoctoNEAR (an easy overestimate to avoid the insufficient-deposit panic).
4. Observe the call succeeds (`Entry::Vacant` path), the mapping is stored, and the contract's account balance permanently retains the extra `1_000_000` yoctoNEAR with no subsequent way for the caller or anyone else to reclaim it, since no method in the contract's public interface (`register`, `lookup`, `get_address`) ever transfers balance out except the collision-refund branch. [2](#0-1)

### Citations

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L36-86)
```rust
    #[payable]
    pub fn register(&mut self, account_id: AccountId) -> Option<String> {
        // It is not allowed to register eth-implicit accounts because the purpose
        // of the registry is to allow looking up the named account associated with
        // an address obtained via hashing, but eth-implicit accounts are already
        // parsable as addresses.
        if is_eth_implicit(&account_id) {
            let log_message = format!("Refuse to register eth-implicit account {account_id}");
            env::log_str(&log_message);
            return None;
        }

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

        let address = account_id_to_address(&account_id);

        match self.addresses.entry(address) {
            Entry::Vacant(entry) => {
                let address = format!("0x{}", hex::encode(address));
                let log_message = format!("Added entry {} -> {}", address, account_id);
                entry.insert(account_id);
                env::log_str(&log_message);
                Some(address)
            }
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
    }
```
