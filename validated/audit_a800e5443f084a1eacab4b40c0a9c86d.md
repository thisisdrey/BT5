Found a valid analog in the `AddressRegistrar` contract, which is part of the ETH-implicit wallet-contract ecosystem in scope.

### Title
Address Registrar `register()` accepts unbounded deposits with no refund path for the excess, permanently freezing user funds - (File: `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs`)

### Summary
`AddressRegistrar::register` only enforces a *lower* bound on the attached deposit (`given_deposit >= required_deposit`) but never validates or caps the *upper* bound, and only refunds the deposit on the collision path. Any deposit in excess of the exact storage cost is silently retained by the contract on the success path, with no withdrawal method anywhere in the contract to recover it.

### Finding Description
`register` computes the exact NEAR needed to store the `address -> account_id` entry: [1](#0-0) 
It then checks only `given_deposit < required_deposit` and panics if the deposit is insufficient — there is no corresponding check that `given_deposit == required_deposit` (or any bound at all on the upper side). On the success path (`Entry::Vacant`), the entire attached deposit becomes part of the contract's account balance and is never sent back to the caller: [2](#0-1) 
Refunding of the deposit is implemented **only** for the collision case (`Entry::Occupied`): [3](#0-2) 
The contract exposes no `withdraw`, owner, or upgrade capability of any kind — `register`, `lookup`, and `get_address` are the entire public interface: [4](#0-3) 
Because NEAR credits an attached deposit directly to the receiving account's balance regardless of contract logic, any amount above `required_deposit` that a caller attaches on a successful (non-colliding) `register` call is added to the contract's balance and can never be moved back out by that caller or anyone else, since no method in the contract ever transfers funds except the collision-refund branch. This is the direct structural analog of the reported bug: a user-facing deposit-accepting entry point that checks a lower bound but has no upper-bound/exact-amount check, so any additional deposited amount is absorbed without limit and without a way to reclaim it.

### Impact Explanation
This is a permanent freezing-of-funds bug for any ordinary, unprivileged account: an over-payment on a successful `register()` call is irrecoverably locked in the `AddressRegistrar` contract's account balance. Given this contract is also invoked as part of the ETH-implicit Wallet Contract's relayer/registrar-lookup flow (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`), a wallet-contract user or a lazy/careless relayer computing the deposit for a `register` call from an Ethereum-style transaction value could similarly cause funds to be irrecoverably stuck.

### Likelihood Explanation
Very high likelihood: this requires no special privilege, no race condition, and no interaction with consensus internals — a single `FunctionCall` action with `method_name: "register"` and a deposit larger than the exact computed storage cost triggers the bug on every successful (non-colliding) registration.

### Recommendation
In `register()`, after computing `required_deposit`, refund any surplus (`given_deposit - required_deposit`) back to `env::predecessor_account_id()` on the success path exactly as is already done on the collision path, or reject the call outright if `given_deposit != required_deposit`.

### Proof of Concept
1. Call `AddressRegistrar::register("alice.near")` attaching a deposit of, e.g., `10 NEAR`, while the actual storage cost (`required_deposit`) for a 40-character account id is a few hundredths of a cent worth of yoctoNEAR.
2. The call succeeds (`Entry::Vacant` branch, see `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs:65-72`), the mapping is stored, and the full `10 NEAR` deposit remains in the contract's balance.
3. There is no method on `AddressRegistrar` to withdraw this balance — confirmed by the contract's complete public interface (`register`, `lookup`, `get_address`, `new`) — so the ~10 NEAR minus the tiny storage cost is permanently frozen. [5](#0-4)

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
