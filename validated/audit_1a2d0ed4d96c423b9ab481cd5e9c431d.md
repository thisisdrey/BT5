### Title
Address Registrar accepts excess attached deposit on successful registration with no refund or withdrawal mechanism, permanently locking user funds - ([File: runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs])

### Summary
The `AddressRegistrar::register` method, part of the ETH-implicit account tooling (NEP-518 Wallet Contract ecosystem), is a `#[payable]` function that only validates the *lower bound* of the attached deposit. When registration succeeds (the `Entry::Vacant` branch), any amount attached above the computed `required_deposit` is silently absorbed into the contract balance instead of being refunded, and the contract exposes no withdrawal function for retrieving these excess funds. This mirrors the "Locked ether" bug class from the external report: a payable entrypoint that checks `attached >= required` but never returns the difference, combined with the absence of any admin/withdraw mechanism.

### Finding Description
`register` computes `required_deposit` from the storage bytes needed to persist the `address -> account_id` mapping, then checks only that `given_deposit >= required_deposit`: [1](#0-0) 

If the check passes and the entry is vacant (the success path), the deposit is never touched again — the account_id is inserted and the function returns without moving or refunding any of `given_deposit`: [2](#0-1) 

Notably, the *only* place the contract explicitly refunds an attached deposit is the collision path (`Entry::Occupied`), where the full `given_deposit` is sent back because "no storage was updated": [3](#0-2) 

This asymmetry demonstrates the developers were aware refunds are needed when the deposit isn't fully consumed by storage, but overlooked the case where a caller overpays yet the registration still succeeds. There is no `withdraw`, owner-only, or otherwise privileged function anywhere in this contract to recover the excess balance later: [4](#0-3) 

Any ordinary NEAR account can call `register` and attach an arbitrarily large deposit (e.g., by mistake, wallet UI rounding, or a relayer/dApp integration bug when calling this contract on behalf of ETH-implicit users via the Wallet Contract's `rlp_execute`). The excess above `required_deposit` becomes permanently part of the contract's balance with no code path to move it back out.

### Impact Explanation
This results in permanent freezing of user funds inside the `AddressRegistrar` contract's account balance. Unlike the generic NEAR runtime refund mechanism — which refunds the entire attached deposit automatically only on receipt/action *failure* (`refund_unspent_gas_and_deposits`, `runtime/runtime/src/lib.rs:1230`) — a successful contract execution that only partially consumes an attached deposit is entirely the contract's responsibility to refund correctly, and this contract fails to do so on its main success path.

### Likelihood Explanation
Likelihood is moderate: any unprivileged user (or a relayer submitting an eth-implicit user's `rlp_execute` call that targets `register`) could easily overpay, especially since callers must estimate `required_deposit` off-chain from `account_id.len()` and `storage_byte_cost()`, both of which are easy to overestimate or supply generously to be safe. No malicious actor is required — normal use patterns (e.g., attaching a flat/rounded deposit like 1 NEAR for margin of safety) trigger the lock.

### Recommendation
In the `Entry::Vacant` success branch, compute `excess = given_deposit.saturating_sub(required_deposit)` and, if non-zero, issue a refund transfer of `excess` back to `env::predecessor_account_id()`, mirroring the refund logic already used in the `Entry::Occupied` branch. Additionally, consider adding an owner-restricted withdrawal method as defense-in-depth for any residual/rounding balance.

### Proof of Concept
1. Deploy `AddressRegistrar` and call `new()`.
2. Compute `required_deposit` for a short `account_id` (e.g., `"a.near"`): `storage_byte_cost * (20 + len(account_id))`.
3. Call `register(account_id)` attaching `required_deposit + 5 NEAR` from `alice.near` (a fresh account_id/address combination so the entry is `Vacant`).
4. Observe: `register` returns `Some(address)` (success), the mapping is stored, and the contract's account balance increases by the full `required_deposit + 5 NEAR` — the 5 NEAR surplus is never returned to `alice.near`.
5. Confirm no method on `AddressRegistrar` exists that can move that surplus balance back out of the contract (see full contract listing at [5](#0-4) ), so the 5 NEAR is permanently locked.

### Citations

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L1-124)
```rust
use near_sdk::{
    borsh::{BorshDeserialize, BorshSerialize},
    env, near_bindgen,
    store::{lookup_map::Entry, LookupMap},
    AccountId, BorshStorageKey, NearToken, PanicOnDefault,
};

type Address = [u8; 20];

#[derive(BorshSerialize, BorshStorageKey)]
#[borsh(crate = "near_sdk::borsh")]
enum StorageKey {
    Addresses,
}

#[near_bindgen]
#[derive(PanicOnDefault, BorshDeserialize, BorshSerialize)]
#[borsh(crate = "near_sdk::borsh")]
pub struct AddressRegistrar {
    pub addresses: LookupMap<Address, AccountId>,
}

#[near_bindgen]
impl AddressRegistrar {
    #[init]
    pub fn new() -> Self {
        Self { addresses: LookupMap::new(StorageKey::Addresses) }
    }

    /// Computes the address associated with the given `account_id` and
    /// attempts to store the mapping `address -> account_id`. If there is
    /// a collision where the given `account_id` has the same address as a
    /// previously registered one then the mapping is NOT updated and `None`
    /// is returned. Otherwise, the mapping is stored and the address is
    /// returned as a hex-encoded string with `0x` prefix.
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

    /// Attempt to look up the account ID associated with the given address.
    /// If an entry for that address is found then the associated account id
    /// is returned, otherwise `None` is returned. Use the `register` method
    /// to add entries to the map.
    /// This function will panic if the given address is not the hex-encoding
    /// of a 20-byte array. The `0x` prefix is optional.
    pub fn lookup(&self, address: String) -> Option<AccountId> {
        let address = {
            let mut buf = [0u8; 20];
            hex::decode_to_slice(address.strip_prefix("0x").unwrap_or(&address), &mut buf)
                .unwrap_or_else(|_| env::panic_str("Invalid hex encoding"));
            buf
        };
        self.addresses.get(&address).cloned()
    }

    /// Computes the address associated with the given `account_id` and
    /// returns it as a hex-encoded string with `0x` prefix. This function
    /// does not update the mapping stored in this contract. If you want
    /// to register an account ID use the `register` method.
    pub fn get_address(&self, account_id: AccountId) -> String {
        let address = account_id_to_address(&account_id);
        format!("0x{}", hex::encode(address))
    }
}

fn account_id_to_address(account_id: &AccountId) -> Address {
    let hash = near_sdk::env::keccak256_array(account_id.as_bytes());
    let mut result = [0u8; 20];
    result.copy_from_slice(&hash[12..32]);
    result
}

fn is_eth_implicit(account_id: &AccountId) -> bool {
    let id = account_id.as_str();
    id.len() == 42 && id.starts_with("0x") && id[2..].chars().all(|c| c.is_ascii_hexdigit())
}
```
