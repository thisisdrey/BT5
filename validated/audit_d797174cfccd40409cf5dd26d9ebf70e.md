### Title
Permanent Freeze of ETH-Implicit Wallet Contract via Unresettable `has_in_flight_tx` Guard on Callback Panic - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The `WalletContract` (the eth-implicit account contract, NEP-518) uses a single boolean flag `has_in_flight_tx` as a "single transaction in flight" guard to serialize execution across its asynchronous promise/callback chain [1](#0-0) . This flag is set to `true` before dispatching a cross-contract call, and is only reset to `false` as the first line of the various `#[private]` callback methods (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`, `ban_relayer`) [2](#0-1) [3](#0-2) [4](#0-3) . Because NEAR contract state changes are only persisted if the receipt executing the function succeeds (a panicking action rolls back all of its `TrieUpdate` changes: `state_update.rollback()` per `runtime/runtime/src/lib.rs:961-970`, confirmed in `protocol-model/spec/runtime-execution.md:70`), if any of these callback functions panics (e.g., runs out of gas) **after** entering the function but **before** returning successfully, the `has_in_flight_tx = false` assignment made at function entry is discarded along with everything else. `has_in_flight_tx` remains permanently `true`, and every subsequent call to `rlp_execute` is unconditionally rejected: [5](#0-4) .

### Finding Description
`rlp_execute` is the sole entry point for interacting with an ETH-implicit account; such accounts cannot have a full-access key added and cannot be deleted (per `docs/DataStructures/Account.md:121`), so this callback path is the *only* way to move funds out of, or otherwise use, the account. The guard flag's intended purpose is to prevent overlapping in-flight transactions, but its unwind-safety assumption is broken: NEAR's execution model treats a panicking receipt as if none of its state writes happened, so the `has_in_flight_tx = false` reset is not durable unless the entire callback function completes without error.

The callbacks receive a fixed ("static") amount of extra gas on top of the user-supplied action gas: `RLP_EXECUTE_CALLBACK_GAS`, `ADDRESS_CHECK_CALLBACK_GAS`, `NEP_141_STORAGE_BALANCE_CALLBACK_GAS` (each 5–15 Tgas) [6](#0-5) . These callbacks call `env::promise_result(0)` and deserialize the returned bytes with `serde_json::from_slice` [7](#0-6) . Because the promised call's target (`target` account, an arbitrary NEP-141 token, or the address registrar reached through `address_check_callback`/`nep_141_storage_balance_callback`) is attacker-influenced — the ETH-emulated `FunctionCall`/`ERC20Transfer` action lets the user pick an arbitrary `receiver_id`/`token_id`/`target` — an attacker can deploy a malicious contract at that target that returns an oversized successful result. Reading and JSON-parsing this oversized payload in the fixed-gas callback can exceed the allotted static gas, causing the callback receipt to fail with an out-of-gas panic. Per the runtime's rollback semantics, this discards the `has_in_flight_tx = false` write, permanently locking the wallet.

This is architecturally analogous to the reported Solidity bug class (state not safely finalized around an external/re-entrant call path leading to an inconsistent guard state), but manifests in nearcore's asynchronous-receipt model as a **stuck reentrancy-guard / DoS on the account's callback state** rather than classic same-frame reentrancy (which does not exist in NEAR's async execution model).

### Impact Explanation
Once `has_in_flight_tx` is stuck at `true`, `rlp_execute` immediately short-circuits with `success: false` and never dispatches a new promise for any future transaction from this ETH-implicit account [8](#0-7) . Since this is the only sanctioned way to move funds or otherwise act from an eth-implicit account (no full-access key can ever be added, the account cannot be deleted), this results in a **permanent freezing of funds** held in that account — funds remain on-chain but become permanently inaccessible to their owner.

### Likelihood Explanation
Reachable by any unprivileged party who controls (or gets the relayer/user to target) a malicious contract as the destination of an eth-emulated `FunctionCall` or `ERC20Transfer` action, or as the address registrar/token lookup target in the `address_check`/`storage_balance` flow. The attacker needs the callback's actual gas usage (base callback allocation + user's specified `action.gas()`) to be exceeded solely by the cost of reading/deserializing the returned payload — achievable by returning a sufficiently large successful result value from the attacker-controlled contract. This requires no protocol-level privilege, only that the wallet owner (or relayer on their behalf) signs a transaction whose target is attacker-controlled, which is plausible for scenarios like emulated ERC-20 transfers to malicious token contracts or `FunctionCall` actions.

### Recommendation
Do not rely on "set flag `false` at function entry" for unwind-safety. Instead, either (a) reset the in-flight guard using a mechanism that is resilient to the callback panicking (e.g., have the runtime treat callback failure paths explicitly and always clear the flag even on `PromiseResult::Failed`/error branches before doing any gas-variable-cost work), or (b) bound/limit the size of data read from `env::promise_result` before it is processed (reject or truncate oversized results cheaply, at near-fixed low gas cost, before attempting deserialization), or (c) allocate callback gas as a function of the possible return-payload size rather than a small fixed constant, and validate that assumption with tests using adversarially large return payloads from a malicious callee contract.

### Proof of Concept
Not executable in this text-only review; conceptually:
1. Attacker deploys a NEAR contract `evil.near` whose method, when called, succeeds and returns an unusually large byte payload (e.g., several hundred KB of JSON).
2. Wallet owner (or a relayer on their behalf) signs and submits, via `rlp_execute`, an ETH-emulated `ERC20Transfer` or `FunctionCall` action targeting `evil.near` (or the address/token lookup path that routes through `evil.near`).
3. `inner_rlp_execute` sets up the promise chain and the top-level call sets `has_in_flight_tx = true` [9](#0-8) .
4. `evil.near`'s call succeeds and returns the oversized payload; the chained callback (`rlp_execute_callback`/`nep_141_storage_balance_callback`) begins executing with only `RLP_EXECUTE_CALLBACK_GAS`/`NEP_141_STORAGE_BALANCE_CALLBACK_GAS` (fixed, small) plus the user-specified `action.gas()`.
5. Reading/deserializing the oversized `PromiseResult::Successful(value)` exhausts the allotted gas before the function returns, causing the callback receipt to fail (panic/out-of-gas).
6. Per NEAR's execution semantics, the failed receipt's state changes (including the `has_in_flight_tx = false` write at function entry) are rolled back; `has_in_flight_tx` remains `true` in the persisted account state.
7. Any subsequent `rlp_execute` call from the legitimate owner is rejected with `"transaction already in progress"`, permanently freezing the account.

Note: I was not able to execute or simulate this scenario against a live nearcore instance to empirically confirm the exact gas thresholds at which the callback panics versus succeeds; this assessment is based on static analysis of the callback gas constants, the promise/callback code, and the documented rollback-on-failure semantics of the runtime. A background Devin session with test-execution capability would be needed to empirically validate the precise payload size required to trigger the panic under realistic gas parameters.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L33-41)
```rust
const NEP_141_STORAGE_DEPOSIT_AMOUNT: NearToken = NearToken::from_yoctonear(1_250 * MICRO_NEAR);
const NEP_141_STORAGE_DEPOSIT_GAS: Gas = Gas::from_tgas(5);
const NEP_141_STORAGE_BALANCE_OF_GAS: Gas = Gas::from_tgas(5);
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L89-105)
```rust
    pub fn rlp_execute(
        &mut self,
        target: AccountId,
        tx_bytes_b64: String,
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L115-127)
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L130-159)
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L276-281)
```rust
    pub fn rlp_execute_callback(
        &mut self,
        caller_deposit: Option<CallerDeposit>,
    ) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        let n = env::promise_results_count();
```
