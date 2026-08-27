Confirmed: the `AddressRegistrar` contract has no `withdraw` function and no `owner`, so overpaid deposits are permanently unrecoverable — a stronger analog than the original report (which at least allowed admin withdrawal). [1](#0-0) 

### Title
Excess attached deposit in `AddressRegistrar::register` is permanently stuck with no refund or withdrawal path - (File: `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs`)

### Summary
`AddressRegistrar::register` is a `#[payable]` method that only validates the deposit is *at least* the required storage cost, but on the success path it never refunds the surplus, and the contract exposes no `withdraw`/owner-controlled function to recover it.

### Finding Description
`register()` computes `required_deposit` from the storage bytes needed to store the new `address -> account_id` entry, then checks `given_deposit < required_deposit` and panics if insufficient [2](#0-1) . However, this only enforces a lower bound — any deposit strictly greater than `required_deposit` passes the check. On the `Entry::Vacant` (success) branch, the code inserts the mapping and returns the address, but never refunds the difference `given_deposit - required_deposit`; the entire attached deposit is retained by the contract balance [3](#0-2) . Contrast this with the `Entry::Occupied` (collision) branch, which explicitly refunds the *entire* `given_deposit` back to the predecessor via a `Transfer` promise because no storage was written [4](#0-3) . This asymmetry shows the developers were aware refunds were needed for over/unused payments, but omitted handling the "partial overpayment on success" case. Furthermore, a search of the contract source confirms there is no `withdraw` method and no owner/admin account defined in `AddressRegistrar` [5](#0-4) , so any surplus deposited during a successful `register` call is permanently locked in the contract's NEAR balance with no code path to ever move it out.

### Impact Explanation
Any ordinary, unprivileged NEAR account (including callers relaying through the eth-implicit wallet contract, since `AddressRegistrar` is part of the `near-wallet-contract` implementation used for eth-implicit account address registration) that attaches more than the minimal required storage deposit when calling `register` permanently loses the excess. Unlike the original Sherlock finding where an admin could eventually call `withdrawFees()` to return misplaced funds, here there is no mechanism at all to retrieve the surplus — it is unconditionally and irreversibly frozen in the contract account's balance. This is a permanent freezing/loss-of-funds condition for the caller.

### Likelihood Explanation
Because `register` is a normal payable public method with no guidance enforcing an exact deposit amount, any caller who over-estimates `storage_byte_cost()` or attaches a round/generous deposit (a common client-side practice to avoid `LackBalanceForState`-style failures) will trigger this loss. This requires no privileged role and no malicious node/peer behavior — it is triggered by an ordinary transaction from the account itself.

### Recommendation
In the `Entry::Vacant` success branch of `register`, compute the surplus `given_deposit.saturating_sub(required_deposit)` and, if non-zero, issue a `promise_batch_action_transfer` refund to `env::predecessor_account_id()`, mirroring the refund logic already implemented in the `Entry::Occupied` branch.

### Proof of Concept
1. Deploy `AddressRegistrar` and call `new()`.
2. Call `register(account_id)` attaching a deposit significantly larger than `storage_byte_cost() * (20 + account_id.len())`, e.g. 1 NEAR when only a few hundred yoctoNEAR-per-byte are required.
3. Observe: the call succeeds (`Entry::Vacant` branch), the mapping is stored, and the full 1 NEAR deposit remains on the `AddressRegistrar` contract account balance.
4. Confirm there is no method on the contract (no `withdraw`, no owner) capable of moving that balance back out — the surplus is permanently unrecoverable, distinguishing this from a temporary/admin-recoverable issue.

### Citations

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L16-21)
```rust
#[near_bindgen]
#[derive(PanicOnDefault, BorshDeserialize, BorshSerialize)]
#[borsh(crate = "near_sdk::borsh")]
pub struct AddressRegistrar {
    pub addresses: LookupMap<Address, AccountId>,
}
```

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
