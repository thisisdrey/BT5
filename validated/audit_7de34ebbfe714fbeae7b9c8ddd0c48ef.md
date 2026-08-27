I found the address-registrar contract's `register` function, which is a good match for the "grant permission cannot be revoked" bug class in-scope for the eth-implicit wallet contract system.## AddressRegistrar `register` — irreversible address→account mapping cannot be corrected

### Summary
The `AddressRegistrar` contract at `runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs` exposes `register(account_id)`, which permanently maps `keccak256(account_id)[12:32] -> account_id` on first-come-first-served basis, with no function to update, remove, or re-register an entry once written. [1](#0-0) [2](#0-1) 

### Finding Description
`register` inserts into `self.addresses: LookupMap<Address, AccountId>` only via `Entry::Vacant` — once an address slot is occupied, `Entry::Occupied` is hit and the call becomes a silent no-op (refunding the deposit), with **no admin, owner, or original-registrant path to overwrite, update, or delete** the mapping: [3](#0-2) [4](#0-3) 

This registry is not decorative — the Wallet Contract's relayer-honesty check (`validate_tx_relayer_data`/`inner_rlp_execute` → `address_check_callback` in `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`) uses `lookup(address)` results to decide whether a `target` eth-implicit account is "really" a named account, and bans the relayer (deletes its access key) if the registrar's answer disagrees with what the relayer claimed: [5](#0-4) [6](#0-5) 

Any address collision — whether from a benign race (two txs registering different accounts whose derived addresses collide, which is astronomically unlikely for keccak256, but also from an account being **renamed/recreated/deleted-and-recreated** such that the *correct* current mapping needs to change — cannot be fixed. Once `register` succeeds for a given 20-byte address, that binding is permanent for the lifetime of the contract, even if the underlying NEAR account is deleted and the account ID becomes available for a different owner, or if the registration was made by mistake/frontrunning (an attacker can race to `register` an account name similar to a victim's before the victim does, permanently poisoning that lookup slot). There is no owner/admin concept in this contract at all — no `isAdmin`-equivalent, so this is a structural, unconditional lack of a revoke/update path, matching the "grant but cannot revoke" bug class from the external report, here manifesting as "bind but cannot rebind/unbind."

### Impact Explanation
Because relayers and users rely on this registrar to determine whether the true `target` of an Ethereum-emulated action is a named NEAR account (versus a raw eth-implicit account), a permanently wrong or stale mapping can cause the Wallet Contract to reject legitimate relayer service (treating honest relayers as faulty via `create_ban_relayer_promise`, deleting their access key) or, in the reverse direction, prevent a legitimately renamed/reassigned account from ever being discoverable through this canonical registrar, degrading interoperability guarantees the wallet-contract flow depends on. This does not directly move funds by itself (the wallet contract's core signature/nonce checks still gate actual value transfer), so it is best characterized as a permanent, unfixable state-poisoning/griefing issue in a production, in-scope, unprivileged-signer-reachable contract (any account can call `register`) rather than direct fund theft. [7](#0-6) 

### Likelihood Explanation
`register` is a public, unprivileged, `#[payable]` method callable by any signer/contract with no access control beyond attaching sufficient storage deposit; the collision (`Entry::Occupied`) path is reached deterministically whenever two account IDs map to the same derived 20-byte address (including the realistic case of an account being deleted and a same-name-derived address being desired again, or deliberate front-running of a `register` call for a target address before the intended legitimate owner registers). This is trivially reachable from any ordinary transaction with no privileged access. [7](#0-6) 

### Recommendation
Add an update/unregister path analogous to the AI Arena `adjustBurningAccess` fix: e.g., allow the registered `account_id`'s signer to call `unregister()` (removing its own entry) before a new registration is possible, or require the caller to prove control of the *currently registered* account (via `predecessor_account_id` check) to overwrite the mapping. At minimum, gate re-registration on the original registrant's continued existence/consent rather than making the binding immutable forever.

### Proof of Concept
1. Attacker observes that account `victim.near` intends to register (or that `victim.near` was deleted and its address slot is now stale).
2. Attacker calls `AddressRegistrar::register("attacker-controlled.near")` where `account_id_to_address("attacker-controlled.near") == account_id_to_address("victim.near")` is contrived, or, more realistically, attacker simply calls `register("victim.near")` variants/predecessor accounts first, or registers before the legitimate account does. [8](#0-7) 
3. Any subsequent call to `register` for a different `account_id` mapping to the same address hits `Entry::Occupied`, logs "Address collision... Keeping the former," refunds the deposit, and permanently leaves the wrong/stale mapping in place: [9](#0-8) 
4. `lookup(address)` will forever return the first-registered (now incorrect or stale) `account_id`, affecting any Wallet Contract flow (`address_check_callback`) that depends on this registrar for target validation, with no on-chain mechanism ever available to correct it.

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

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L36-46)
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
```

**File:** runtime/near-wallet-contract/implementation/address-registrar/src/lib.rs (L63-86)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L130-191)
```rust
    /// Callback after checking if an address is contained in the registrar.
    /// This check happens when the target is another eth implicit account to
    /// confirm that the relayer really did check for a named account with that address.
    #[private]
    pub fn address_check_callback(
        &mut self,
        target: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
        let maybe_account_id: Option<AccountId> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Call to Address Registrar contract failed".into()),
                });
            }
            PromiseResult::Successful(value) => match serde_json::from_slice(&value) {
                Ok(x) => x,
                Err(_) => {
                    return PromiseOrValue::Value(ExecuteResponse {
                        success: false,
                        success_value: None,
                        error: Some("Unexpected response from account registrar".into()),
                    });
                }
            },
        };
        let current_account_id = env::current_account_id();
        let promise = if maybe_account_id.is_some() {
            // We intentionally do not increment the nonce in this case because the
            // error is caused by a faulty relayer, not the user. An honest relayer
            // may still be able to successfully send the user's intended transaction.
            if env::signer_account_id() == current_account_id {
                create_ban_relayer_promise(current_account_id)
            } else {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Invalid target: target is address corresponding to existing named account_id".into()),
                });
            }
        } else {
            // We must increment the nonce at this point to prevent replay of the transaction.
            // Recall that the nonce was not incremented in `inner_rlp_execute` in the case that
            // the registrar contract was called (i.e. in the case we end up inside this callback).
            self.nonce = self.nonce.saturating_add(1);
            let ext =
                WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
            match action_to_promise(target, action)
                .map(|p| p.then(ext.rlp_execute_callback(caller_deposit)))
            {
                Ok(p) => p,
                Err(e) => {
                    return PromiseOrValue::Value(e.into());
                }
            }
        };
        self.has_in_flight_tx = true;
        PromiseOrValue::Promise(promise)
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L66-82)
```rust
    // The way an honest relayer assigns `target` is as follows:
    // 1. If the Ethereum transaction payload represents a Near action then use the receiver_id,
    // 2. If the payload looks like a supported Ethereum emulation then use the address registrar:
    // 2.a. if the tx.to address is registered then use the associated account id,
    // 2.b. otherwise, tx.to == target
    // 3. Otherwise, tx.to == target
    // Given this algorithm, the only way to have `TargetKind::EthImplicit` is in the
    // following cases:
    // I)   The Ethereum transaction payload is not parseable as a known action,
    // II)  The payload is parsable as a Near action and the receiver_id is an eth-implicit account
    // III) The payload is parsable as a supported Ethereum emulation but the to address is
    //      not registered in the address registrar.
    // Therefore, to determine if the relayer is honest we must always parse the payload and
    // we only need to check the registrar if the payload is parseable as an Ethereum emulation.
    // Note: the `TargetKind` is determined in `validate_tx_relayer_data` above, and that function
    // also confirms that the `target` is compatible with the user's `tx.to`.

```
