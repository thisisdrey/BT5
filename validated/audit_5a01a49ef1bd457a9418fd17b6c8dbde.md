### Title
Wallet contract permanently bricked ("in-flight" flag stuck) when an async callback panics before committing - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
The eth-implicit `WalletContract` uses a `has_in_flight_tx` boolean to serialize execution: `rlp_execute` refuses to start a new transaction while a previous one's promise chain hasn't resolved, and the flag is only cleared inside the async callbacks (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`, `ban_relayer`). Because NEAR rolls back *all* state changes of a failed receipt atomically, any panic occurring in one of these callbacks after the flag-reset line but before the receipt finishes successfully undoes the reset, leaving `has_in_flight_tx == true` forever. Since ETH-implicit accounts have no access key and can only be driven through this contract, this permanently freezes the account and any funds/promises associated with it — the same failure mode as the reported auction-house bug, where a callback that can revert bricks the whole flow and locks funds.

### Finding Description
`WalletContract` explicitly documents the invariant that `has_in_flight_tx` must be `true` exactly while a promise from a mutable method is outstanding, and `rlp_execute` enforces "only one transaction at a time" by refusing any call while the flag is set: [1](#0-0) [2](#0-1) 

The flag is only ever cleared as the first statement of the four callback methods: [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

Each of these callback bodies continues on to do more work after that first line: parsing an untrusted contract's JSON response, building further promises (`action_to_promise`, `promise_batch_action_transfer`, `WalletContract::ext(...).with_static_gas(...)`), etc. `nep_141_storage_balance_callback` in particular can chain up to three actions (`storage_balance_of` lookup → `storage_deposit` → the actual transfer → callback), all funded from a hard-coded, small gas budget (`NEP_141_STORAGE_BALANCE_CALLBACK_GAS`): [7](#0-6) [8](#0-7) 

NEAR's runtime treats an `ActionReceipt`'s execution as all-or-nothing: if any action inside it fails (including a WASM host error such as gas exhaustion, or any panic), the whole receipt is marked `Err` and the runtime rolls back every state mutation the receipt made, per the documented atomicity invariant ("Failed receipt atomicity ... a receipt whose result is Err triggers state_update.rollback(), so no state changes persist except the outcome/gas accounting"): [9](#0-8) 

Because the flag-clearing write and the rest of the callback logic live in the *same* receipt/action, any panic later in that same callback (e.g., `GasExceeded` from underestimated static gas, a host error while composing the next promise, or an unhandled panic while processing a malicious external contract's response) discards the `has_in_flight_tx = false` write along with everything else. `rlp_execute` will then refuse all future calls forever, since it checks the flag before doing anything else, and there is no other entry point or access key on an eth-implicit account to reset it (per `accounts-keys.md`, ETH-implicit accounts are created with a `Global` wallet contract or legacy embedded code — not a full-access key — so this contract is the only way to act on the account).

### Impact Explanation
This permanently freezes the ETH-implicit wallet account: `rlp_execute` will reject every subsequent call because `has_in_flight_tx` can never be cleared again, and since there is no access key on the account, there is no alternative path to recover it. Any balance held by the account, and any pending/incomplete cross-contract action, becomes permanently inaccessible — the same class of impact (permanent freezing of funds / bricked contract) as the reported auction-house issue.

### Likelihood Explanation
Reaching a panic partway through one of these callbacks is plausible without any special privilege: the callback gas budgets are small, fixed constants (5–15 Tgas) that must cover deserialization of attacker/registrar/token-controlled data plus construction of subsequent promises; an external NEP-141 token or the address registrar returning an unexpectedly large/complex response, or gas pricing drift, can exhaust the budget and trigger `GasExceeded` inside the callback receipt, after the flag reset but before the receipt completes. Any ordinary user relying on this ETH-emulation path (ERC-20 transfers with the two-hop `storage_balance_of`/`storage_deposit` flow, or the address-registrar lookup flow) is exposed, and no special access is required beyond normal use of the wallet.

### Recommendation
Do not rely on same-receipt atomicity to persist the "in-flight" flag reset. Options:
- Reset `has_in_flight_tx` in a dedicated, minimal-logic action/receipt that runs and commits before any additional fallible logic executes (e.g., split "clear flag" into its own promise/receipt that cannot fail).
- Add a timeout/expiry mechanism (e.g., store the block height/timestamp when the flag was set) so that if a callback never completes successfully within a bound, subsequent calls can safely proceed instead of being permanently blocked.
- Increase and pad callback gas budgets generously, and audit all fallible operations in the callbacks to ensure they cannot panic; make failure paths return `ExecuteResponse` values rather than causing host errors that trigger rollback.

### Proof of Concept
Conceptual PoC (cannot be executed without a live NEAR sandbox environment, but the code path is deterministic):
1. Deploy an ETH-implicit account backed by `WalletContract`.
2. Submit an RLP transaction whose `target`/`action` is an `ERC20Transfer` to a NEP-141 token contract that either has an unusually slow/expensive `storage_balance_of`, or otherwise causes `nep_141_storage_balance_callback` to consume more gas than `NEP_141_STORAGE_BALANCE_CALLBACK_GAS` allows once it reaches the point of chaining `storage_deposit` + transfer + `rlp_execute_callback`.
3. Observe that the callback receipt fails with a `FunctionCallError`/`GasExceeded` (or any other panic) after `self.has_in_flight_tx = false;` executed in-memory but before the receipt commits.
4. Per the runtime's atomic rollback of failed receipts [9](#0-8) , the contract's stored `has_in_flight_tx` remains `true`.
5. Any subsequent call to `rlp_execute` is rejected with `"transaction already in progress"` [10](#0-9) , permanently, since there is no other entry point to reset the flag.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L36-41)
```rust
const REGISTRAR_LOOKUP_GAS: Gas = Gas::from_tgas(5);
const RLP_EXECUTE_CALLBACK_GAS: Gas = Gas::from_tgas(5);
const ADDRESS_CHECK_CALLBACK_GAS: Gas = Gas::from_tgas(5).saturating_add(RLP_EXECUTE_CALLBACK_GAS);
const NEP_141_STORAGE_BALANCE_CALLBACK_GAS: Gas = Gas::from_tgas(5)
    .saturating_add(NEP_141_STORAGE_DEPOSIT_GAS)
    .saturating_add(RLP_EXECUTE_CALLBACK_GAS);
```

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L96-120)
```rust
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
        let current_account_id = env::current_account_id();
        let predecessor_account_id = env::predecessor_account_id();
        let result = inner_rlp_execute(
            current_account_id.clone(),
            predecessor_account_id,
            target,
            tx_bytes_b64,
            &mut self.nonce,
        );

        match result {
            Ok(promise) => {
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L133-141)
```rust
    #[private]
    pub fn address_check_callback(
        &mut self,
        target: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
        let maybe_account_id: Option<AccountId> = match env::promise_result(0) {
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L194-203)
```rust
    #[private]
    pub fn nep_141_storage_balance_callback(
        &mut self,
        token_id: AccountId,
        receiver_id: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
        let maybe_storage_balance: Option<StorageBalance> = match env::promise_result(0) {
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L223-269)
```rust
        let ext = WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
        let promise = match maybe_storage_balance {
            Some(_) => {
                // receiver_id is registered so we can send the transfer
                // without additional actions. Note: in the standard NEP-141
                // implementation it is impossible to have `Some` storage balance,
                // but have it be insufficient to transact.
                match action_to_promise(token_id, action)
                    .map(|p| p.then(ext.rlp_execute_callback(caller_deposit)))
                {
                    Ok(p) => p,
                    Err(e) => {
                        return PromiseOrValue::Value(e.into());
                    }
                }
            }
            None => {
                // receiver_id is not registered so we must call `storage_deposit` first.
                let storage_deposit_args =
                    format!(r#"{{"account_id": "{receiver_id}"}}"#).into_bytes();
                let transfer_function_call = match action {
                    near_action::Action::FunctionCall(x) => x,
                    _ => {
                        return PromiseOrValue::Value(ExecuteResponse {
                            success: false,
                            success_value: None,
                            error: Some(
                                "Expected function call action to perform NEP-141 transfer".into(),
                            ),
                        });
                    }
                };
                Promise::new(token_id)
                    .function_call(
                        "storage_deposit".into(),
                        storage_deposit_args,
                        NEP_141_STORAGE_DEPOSIT_AMOUNT,
                        NEP_141_STORAGE_DEPOSIT_GAS,
                    )
                    .function_call(
                        transfer_function_call.method_name,
                        transfer_function_call.args,
                        transfer_function_call.deposit,
                        transfer_function_call.gas,
                    )
                    .then(ext.rlp_execute_callback(caller_deposit))
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L276-285)
```rust
    pub fn rlp_execute_callback(
        &mut self,
        caller_deposit: Option<CallerDeposit>,
    ) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        let n = env::promise_results_count();

        if n == 0 {
            // `rlp_execute_callback` is called directly in the case of an emulated self-transfer.
            return ExecuteResponse { success: true, success_value: None, error: None };
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

**File:** runtime/runtime/src/lib.rs (L946-951)
```rust
                // TODO storage error
                if let Err(ref mut res) = result.result {
                    res.index = Some(action_index as u64);
                    break;
                }
            }
```
