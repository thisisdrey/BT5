### Title
Excess attached deposit is not refunded when registering an address in `AddressRegistrar::register` - (File: `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs`)

### Summary
`AddressRegistrar::register`, used by the eth-implicit wallet contract to store the `keccak(account_id) -> account_id` mapping, only validates that the caller's attached deposit meets the minimum storage cost. It never checks for or refunds any deposit in excess of that minimum on the success path, so any overpayment is silently and permanently retained by the contract.

### Finding Description
`register` computes the exact storage cost required (`required_deposit`) and panics if `given_deposit < required_deposit` [1](#0-0) . This mirrors the reported bug pattern exactly: it enforces a *minimum* but never an *exact-amount* or excess check.

On the success (`Entry::Vacant`) path, the full `given_deposit` is implicitly kept by the contract — only `account_id` is inserted into the map, with no logic to compute or refund `given_deposit - required_deposit`: [2](#0-1) 

Contrast this with the collision (`Entry::Occupied`) branch, which explicitly refunds the *entire* deposit back to the predecessor because it correctly recognizes no storage was consumed: [3](#0-2) 

The asymmetry shows the developers were aware refunding is necessary when the deposit isn't fully "used," but on the success path they only refund 0 (nothing) instead of `given_deposit - required_deposit`. There is no `withdraw`/owner-controlled sweep method anywhere else in the contract [4](#0-3)  to later recover this stuck balance — the contract only exposes `new`, `register`, `lookup`, and `get_address` [5](#0-4) .

This contract is invoked in the eth-implicit wallet's execution flow, e.g., exercised in `wallet-contract`'s cross-contract-call tests, confirming it is reachable via an ordinary (non-privileged) RLP-signed transaction from an eth-implicit account: [6](#0-5) 

### Impact Explanation
Any caller (an eth-implicit account driving a `FunctionCall` with `#[payable]` `register`, or any regular NEAR account calling it directly) that attaches more NEAR than the exact byte-cost of storing the mapping permanently loses the excess — it is neither refunded nor recoverable by any other contract method. Since eth-implicit/EVM-style callers typically cannot predict the exact yoctoNEAR storage price and naturally overpay for safety margin (directly analogous to the original report's "user supplies 1000 ARCH when 58 is needed"), this results in concrete, permanent loss of user funds with no compensating withdrawal path.

### Likelihood Explanation
Likelihood is high in practice: `storage_byte_cost()`-based exact-deposit calculation is fragile for callers (especially cross-VM callers going through the wallet contract's RLP-encoded transactions), so overpayment to ensure the call doesn't panic on insufficient deposit is a natural and common client behavior, just as in the original ARCH-overpayment report.

### Recommendation
After successfully inserting the new mapping in the `Entry::Vacant` branch, compute `excess = given_deposit - required_deposit` and, if `excess > 0`, issue a `promise_batch_action_transfer` refund of `excess` back to `env::predecessor_account_id()`, mirroring the refund already implemented in the `Entry::Occupied` branch.

### Proof of Concept
1. Compute `required_deposit = storage_byte_cost * (20 + len(account_id))` for a target `account_id` (e.g., `alice.near`).
2. Call `register("alice.near")` attaching a deposit noticeably larger than `required_deposit` (e.g., 10x), either directly or via the eth-implicit wallet contract's RLP-signed `FunctionCall` action (as exercised in `test_caller_refunds`, `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs:170-229`).
3. Observe the call succeeds (`Entry::Vacant` branch), `Some(address)` is returned, and the contract balance increases by the *entire* attached deposit rather than only `required_deposit`.
4. Confirm there is no method to reclaim the excess — the caller's overpayment is permanently locked in the contract.

### Citations

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L1-21)
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
```

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L23-112)
```rust
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L170-229)
```rust
// An external caller gets its deposit back if the cross-contract call fails.
#[tokio::test]
async fn test_caller_refunds() -> anyhow::Result<()> {
    let TestContext { worker, wallet_contract, wallet_sk, address_registrar, .. } =
        TestContext::new().await?;

    let caller = worker.root_account()?;
    let deposit_amount = NearToken::from_near(3);
    let create_tx = |receiver_id: &AccountId, nonce: u64| {
        let method = "register";
        let args = br#"{"account_id": "birchmd.near"}"#;
        let action = Action::FunctionCall {
            receiver_id: receiver_id.to_string(),
            method_name: method.into(),
            args: args.to_vec(),
            gas: Gas::from_tgas(10).as_gas(),
            yocto_near: 0,
        };
        utils::create_signed_transaction(
            nonce,
            receiver_id,
            Wei::new_u128(deposit_amount.as_yoctonear() / (MAX_YOCTO_NEAR as u128)),
            action,
            &wallet_sk,
        )
    };

    // External caller gets a refund when the cross-contract call fails
    let pre_tx_account_balance = caller.view_account().await?.balance;
    let receiver_id: AccountId = "fake.near".parse()?;
    let result = wallet_contract
        .rlp_execute_from(
            &caller,
            receiver_id.as_str(),
            &create_tx(&receiver_id, 0),
            deposit_amount,
        )
        .await?;
    assert!(!result.success);
    let post_tx_account_balance = caller.view_account().await?.balance;
    assert!(
        pre_tx_account_balance.as_yoctonear() - post_tx_account_balance.as_yoctonear()
            < deposit_amount.as_yoctonear()
    );

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

    Ok(())
}
```
