## Analysis

The external report describes an AMM pool whose `burn()` (withdrawal) path depends on an external "hook" call; if the hook contract misbehaves, the whole withdrawal reverts and the user's funds get stuck, with no fallback exit. The closest unprivileged-signer analog in nearcore is the **ETH-implicit Wallet Contract**, which is the *only* way to operate an ETH-implicit account (it cannot receive a full-access key and cannot be deleted, per [1](#0-0) ). Every mutating call gated by a single boolean flag, `has_in_flight_tx`, that is guarded to prevent concurrent execution and is only ever cleared inside asynchronous cross-contract callbacks.

### Root cause

`rlp_execute` immediately rejects any call while `has_in_flight_tx` is `true`, with no other code path to reset it: [2](#0-1) 

For a NEP-141 (`ERC20Transfer`) emulation, the contract calls an arbitrary target token contract's `storage_balance_of` and chains a `.then()` callback (`nep_141_storage_balance_callback`) with a fixed, small static gas budget: [3](#0-2) 

The callback deserializes the *token contract's* response before doing anything else useful, but the first statement that clears the flag runs, then deserialization happens afterward: [4](#0-3) 

The same pattern exists in `address_check_callback` for the address-registrar hook: [5](#0-4) 

Because a NEAR function-call receipt is atomic, if the callback function panics for *any* reason before returning successfully (e.g. it runs out of the small static gas allotted to it while deserializing an oversized/malformed JSON payload returned by the external token/registrar contract), **all state changes made during that receipt — including the earlier `self.has_in_flight_tx = false` — are rolled back**. The contract's own doc comment states the invariant that must hold, implicitly acknowledging that failure to restore it is a correctness risk: [6](#0-5) 

Since `target`/`token_id` in the `ERC20Transfer` path is effectively chosen by whichever contract the user's signed Ethereum transaction points to (validated only by an address/hash match in `validate_tx_relayer_data`, not by any allow-list), any external contract the user (or a relayer on their behalf) ever interacts with can act as the "broken hook." If that contract returns a hostile/oversized response from `storage_balance_of` (or the address registrar's `lookup`), the callback exhausts its static gas budget and panics, and `has_in_flight_tx` is never durably cleared.

Once stuck at `true`, `rlp_execute` will refuse **every** future call — including a perfectly valid, correctly-signed transaction from the legitimate owner — because the check at the top of `rlp_execute` short-circuits before any signature/nonce validation is even attempted. There is no emergency/administrative exit method exposed by the contract (only `ban_relayer`, which itself is only reachable through the same gated `rlp_execute`/callback flow), so all $NEAR and any other assets custodied by that ETH-implicit account become **permanently unreachable**, matching the "permanent freezing of funds" impact category.

### Title
Unresettable `has_in_flight_tx` flag in the ETH-implicit Wallet Contract permanently freezes user funds if a cross-contract callback hook panics - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Wallet Contract that exclusively controls every ETH-implicit account gates all mutating entry points behind a `has_in_flight_tx` flag that is only cleared inside asynchronous `.then()` callbacks chained to external "hook" contracts (an address registrar, or an arbitrary NEP-141 token contract). If such a callback panics before completing — for example because it is given a hostile/oversized response by the external contract and exhausts its small statically-allocated gas budget while deserializing it — the entire receipt (including the flag reset) is rolled back atomically. The flag remains `true` forever, and `rlp_execute` rejects all subsequent calls unconditionally, with no other way to interact with the account.

### Finding Description
`rlp_execute` is the sole entry point for ETH-implicit accounts ( [7](#0-6) ), and it bails out immediately when `has_in_flight_tx` is `true`, without validating the signature or nonce of the caller. The flag is set to `true` right before dispatching a cross-contract promise chain (lines 118, 123, 190, 271) and is only cleared as the *first* statement of the corresponding callback (`address_check_callback` line 140, `nep_141_storage_balance_callback` line 202, `rlp_execute_callback` line 280, `ban_relayer` line 321). Because callback execution is atomic, if the callback subsequently panics (e.g. gas exhaustion while parsing `env::promise_result(0)` from an external contract that returned a malicious/huge payload), the flag-clearing mutation is discarded along with the rest of the failed receipt's state changes. The external contract being called (address registrar for `EOABaseTokenTransfer`, or arbitrary token contract for `ERC20Transfer`) is effectively an unprivileged, potentially-adversarial "hook" from the wallet contract's perspective, matching the bug class in the external report.

### Impact Explanation
Once `has_in_flight_tx` is stuck `true`, the account becomes permanently unusable: no transfer, function call, or key management action can ever be executed again, because the guard check runs before any user-authentication logic. Since ETH-implicit accounts cannot be granted a full-access key and cannot be deleted (per `docs/DataStructures/Account.md`), there is no alternative recovery path. Any $NEAR balance or contract-held assets tied to that account are permanently frozen — a direct, severe funds-freezing impact.

### Likelihood Explanation
Triggering this requires the wallet-contract owner (or a relayer acting for them) to interact with an external contract (a NEP-141 token, or in principle the fixed address registrar if it misbehaves) that returns a response large or malformed enough to make the fixed-gas callback panic before finishing. This is plausible for any non-standard, buggy, or intentionally malicious token contract a user might hold/interact with — the target is not restricted to a vetted allow-list. It does not require validator or protocol-level privilege, only ordinary transaction submission, matching an "unprivileged signer" reachable path.

### Recommendation
Ensure the "in-flight" flag is cleared defensively and is not lost on callback panics — e.g. move the flag reset to occur via a low-level mechanism guaranteed to persist even if the rest of the callback body fails (such as a dedicated minimal callback purely responsible for clearing the flag, executed with a gas reservation independent of untrusted external data processing), or bound/validate the size of promise results before deserializing them so that gas exhaustion cannot occur mid-callback. Additionally, provide an owner-authenticated emergency-reset path (e.g., one that only requires validating the Ethereum signature/nonce without depending on `has_in_flight_tx`) so a stuck flag can be recovered without permanently freezing the account.

### Proof of Concept
1. User (or relayer on their behalf) signs an Ethereum-style transaction routed through `rlp_execute` with `target` set to a NEP-141 token contract, taking the `ERC20Transfer` path (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:433-458`).
2. The token contract's `storage_balance_of` (called as the "hook") returns an oversized/malformed byte payload as the promise result.
3. `nep_141_storage_balance_callback` sets `has_in_flight_tx = false` (line 202), then attempts `serde_json::from_slice(&value)` on the hostile payload (lines 211-220), consuming more gas than `NEP_141_STORAGE_BALANCE_CALLBACK_GAS` and causing the receipt to fail with an out-of-gas panic.
4. Because the receipt failed, all its state mutations — including the flag reset — are rolled back; `has_in_flight_tx` persists as `true` in contract state.
5. Any subsequent call to `rlp_execute`, even from the legitimate key holder with a valid, correctly-nonced transaction, is rejected at lines 97-105 before any further logic runs, permanently locking the account.

### Citations

**File:** docs/DataStructures/Account.md (L119-122)
```markdown
Once a NEAR-implicit account is created it acts as a regular account until it's deleted.

An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-105)
```rust
    #[payable]
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L134-159)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L201-221)
```rust
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
        let maybe_storage_balance: Option<StorageBalance> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some(format!("Call to NEP-141 {token_id}::storage_balance_of failed")),
                });
            }
            PromiseResult::Successful(value) => match serde_json::from_slice(&value) {
                Ok(x) => x,
                Err(_) => {
                    return PromiseOrValue::Value(ExecuteResponse {
                        success: false,
                        success_value: None,
                        error: Some("Unexpected response from NEP-141 storage_balance_of".into()),
                    });
                }
            },
        };
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L433-458)
```rust
        TransactionKind::EthEmulation(EthEmulationKind::ERC20Transfer { receiver_id, .. }) => {
            // In the case of the emulated ERC-20 transfer, the receiving account
            // might not be registered with the NEP-141 contract (per the NEP-145)
            // storage standard. Therefore we must create a multi-step promise where
            // first we check if the receiver is registered and then if not call
            // `storage_deposit` in addition to `ft_transfer`.
            let token_id = target;
            let callback_gas = NEP_141_STORAGE_BALANCE_CALLBACK_GAS.saturating_add(action.gas());
            let ext: WalletContractExt =
                WalletContract::ext(current_account_id).with_static_gas(callback_gas);
            let storage_balance_args =
                format!(r#"{{"account_id": "{}"}}"#, receiver_id.as_str()).into_bytes();
            Promise::new(token_id.clone())
                .function_call(
                    "storage_balance_of".into(),
                    storage_balance_args,
                    NearToken::from_yoctonear(0),
                    NEP_141_STORAGE_BALANCE_OF_GAS,
                )
                .then(ext.nep_141_storage_balance_callback(
                    token_id,
                    receiver_id,
                    action,
                    caller_deposit,
                ))
        }
```
