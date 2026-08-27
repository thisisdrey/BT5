### Title
Wallet Contract's `has_in_flight_tx` guard can become permanently stuck, freezing an ETH-implicit account - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The NEAR Wallet Contract (deployed as the sole controller of every ETH-implicit account) uses a boolean flag `has_in_flight_tx` to prevent concurrent execution of `rlp_execute`. Every code path that sets the flag to `true` is documented as required to eventually reset it to `false` via a follow-up callback. One path — the "ban relayer" flow — batches two actions (`delete_key` + `function_call_weight("ban_relayer")`) into a single, un-chained promise/receipt instead of using `.then()`. If the first action in that batched receipt fails or the receipt runs out of gas before the second action executes, `ban_relayer` (the only handler that resets the flag on this path) never runs, and `has_in_flight_tx` is permanently stuck at `true`. Because `rlp_execute` immediately short-circuits with an error whenever `has_in_flight_tx` is `true`, this permanently disables all future interaction with the account.

### Finding Description
`WalletContract::rlp_execute` guards re-entrancy with a single boolean: [1](#0-0) 

The struct's own doc comment states the invariant that must hold for the contract to remain usable: [2](#0-1) 

When `inner_rlp_execute` produces a `Relayer` error and the signer key belongs to the account itself, the contract sets `has_in_flight_tx = true` and dispatches `create_ban_relayer_promise`: [3](#0-2) 

`create_ban_relayer_promise` builds a single promise batch (one receipt) containing `delete_key` followed by `function_call_weight("ban_relayer")`, rather than chaining a separate callback with `.then()`: [4](#0-3) 

`ban_relayer` is the only handler on this path that resets the flag back to `false`: [5](#0-4) 

Because the two actions are bundled into one receipt instead of being separated by `.then()`, they are executed sequentially as part of the same action receipt. If the `delete_key` action fails (or the receipt runs out of prepaid gas before reaching the `function_call_weight` action), the whole receipt fails and `ban_relayer` never executes — so `has_in_flight_tx` is never reset. Every subsequent `rlp_execute` call then immediately short-circuits: [6](#0-5) 

Since ETH-implicit accounts can only ever be operated through the deployed Wallet Contract (no full-access key can be added, and the account cannot be deleted), this is functionally identical to the Mooniswap "cannot unpause" bug class: a boolean lock/pause flag with a code path that can flip it on but has no way of ever flipping it back off, permanently disabling the contract's primary function. [7](#0-6) 

### Impact Explanation
If `has_in_flight_tx` becomes permanently stuck at `true`, the affected ETH-implicit account can never again execute `rlp_execute`, which is the only entry point for `Transfer`, `FunctionCall`, `AddKey`, and `DeleteKey` actions on that account. Any $NEAR balance or fungible tokens controlled by that ETH-implicit account become permanently frozen — unrecoverable by the owner, the relayer, or anyone else — matching the "permanent freezing of funds" impact class.

### Likelihood Explanation
This path is only reached in the "self-relay" scenario (`env::signer_account_id() == current_account_id`, i.e., the account owner is using their own access key as their own relayer) combined with a malformed/relayer-class RLP transaction that surfaces `Error::Relayer`. Triggering the actual freeze additionally requires the batched `delete_key` + `function_call_weight` receipt to fail before the second action runs (e.g., insufficient attached/prepaid gas budget, or a state where the key no longer matches). This narrows the practical likelihood, but the path is reachable by an ordinary account owner/relayer without any privileged access, through normal transaction construction (e.g. deliberately or accidentally under-provisioning gas for a self-relayed malformed transaction).

### Recommendation
Chain the `delete_key` action with the `ban_relayer` call via `.then()` (as is done for every other path in this contract, e.g. `rlp_execute_callback`), so `ban_relayer` executes — and resets `has_in_flight_tx` — as a callback regardless of whether the `delete_key` action succeeds or fails. Additionally, consider adding a governance/self-recovery mechanism (e.g., a timeout-based reset of `has_in_flight_tx`) so that any unforeseen failure to reset the flag cannot permanently lock the account.

### Proof of Concept
1. Create an ETH-implicit account and add its own signing key as a `FunctionCall`-permissioned access key restricted to `rlp_execute` (a common "self-relay" pattern) as shown in [8](#0-7) .
2. Submit a `rlp_execute` transaction with an RLP payload engineered to fail parsing/validation such that `inner_rlp_execute` returns `Error::Relayer`, and attach only enough gas to cover the `delete_key` action but not the subsequent `function_call_weight("ban_relayer")` action in `create_ban_relayer_promise`.
3. Observe that the resulting receipt exhausts gas (or otherwise fails) after `delete_key` but before `ban_relayer` executes, leaving `has_in_flight_tx = true` in the contract's persisted state.
4. Submit any subsequent `rlp_execute` transaction and observe it is immediately rejected with `"Error: transaction already in progress, please try again later."` indefinitely, since no code path remains to reset the flag.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L46-55)
```rust
pub struct WalletContract {
    pub nonce: u64,
    /// Tracks whether a transaction is currently being executed
    /// (i.e. has receipts that have not yet resolved).
    /// Invariant: `has_in_flight_tx` must be `true` when a mutable method
    /// of this contract returns a promise and `false` otherwise (except
    /// for the check if a transaction is already in flight at the beginning
    /// of `rlp_execute`).
    pub has_in_flight_tx: bool,
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L93-105)
```rust
    ) -> PromiseOrValue<ExecuteResponse> {
        // To ensure user actions are executed in the desired order,
        // having multiple transactions in flight at the same time is
        // not allowed.
        if self.has_in_flight_tx {
            return PromiseOrValue::Value(ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(
                    "Error: transaction already in progress, please try again later.".into(),
                ),
            });
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L116-127)
```rust
        match result {
            Ok(promise) => {
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
                let promise = create_ban_relayer_promise(current_account_id);
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(e) => PromiseOrValue::Value(e.into()),
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L319-327)
```rust
    #[private]
    pub fn ban_relayer(&mut self) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        ExecuteResponse {
            success: false,
            success_value: None,
            error: Some("Error: faulty relayer".into()),
        }
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L503-512)
```rust
fn create_ban_relayer_promise(current_account_id: AccountId) -> Promise {
    let pk = env::signer_account_pk();
    Promise::new(current_account_id).delete_key(pk).function_call_weight(
        "ban_relayer".into(),
        Vec::new(),
        NearToken::from_yoctonear(0),
        Gas::from_tgas(1),
        GasWeight(1),
    )
}
```

**File:** docs/DataStructures/Account.md (L119-122)
```markdown
Once a NEAR-implicit account is created it acts as a regular account until it's deleted.

An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
```

**File:** integration-tests/src/tests/features/wallet_contract.rs (L280-304)
```rust
    // The relayer adds its key to the eth implicit account so that
    // can sign Near transactions for the user.
    let relayer_pk = relayer_signer.signer.public_key();
    let action = Action::AddKey(Box::new(AddKeyAction {
        public_key: relayer_pk,
        access_key: AccessKey {
            nonce: 0,
            permission: AccessKeyPermission::FunctionCall(FunctionCallPermission {
                allowance: None,
                receiver_id: eth_implicit_account.to_string(),
                method_names: vec!["rlp_execute".into()],
            }),
        },
    }));
    let signed_transaction = create_rlp_execute_tx(
        &eth_implicit_account,
        action,
        0,
        &eth_implicit_account,
        &secret_key,
        &mut relayer_signer,
        &env,
    );
    let prepaid_gas = total_prepaid_gas(signed_transaction.transaction.actions()).unwrap();
    height = check_tx_processing(&mut env, signed_transaction, height, blocks_number);
```
