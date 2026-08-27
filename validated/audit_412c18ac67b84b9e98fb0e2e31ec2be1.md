`max_length_returned_data` is `4_194_304` bytes (4 MiB). This confirms a malicious NEP-141 token can return a payload up to 4 MiB from `storage_balance_of`, deserialized inside `nep_141_storage_balance_callback` before `has_in_flight_tx` is durably reset.

### Title
Malicious NEP-141 token contract can permanently lock an eth-implicit Wallet Contract account by causing its callback to trap before `has_in_flight_tx` is committed - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The eth-implicit Wallet Contract (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`) uses an in-flight-transaction flag, `has_in_flight_tx`, to serialize the owner's asynchronous transactions [1](#0-0) . This flag is only reset when a `#[private]` callback (`nep_141_storage_balance_callback`, `address_check_callback`, `rlp_execute_callback`) successfully completes execution and its state write commits. Because NEAR's account-state commit model discards all state mutations of a receipt whose execution traps/panics, any panic occurring later in the same callback invocation silently undoes the `self.has_in_flight_tx = false;` write already made at the top of the function [2](#0-1) , leaving the flag stuck at `true`. `rlp_execute` unconditionally rejects any further transaction whenever `has_in_flight_tx` is `true`, with no other function able to clear it [3](#0-2) .

This is structurally analogous to the Astaria bug: an attacker who is invited into a cross-call (there, an arbitrary lien-token receiver; here, an arbitrary target contract chosen by the transaction's `to` field, e.g., an ERC-20/NEP-141 token) can make that call misbehave (there: revert via `supportsInterface`; here: return an oversized/crafted payload that causes an out-of-gas trap during deserialization) in order to break a protocol invariant relied on by the victim (there: auction/liquidation/payment completion; here: the single-flight guard that gates all future use of the account).

### Finding Description
The relevant flow:
1. The wallet owner signs an Ethereum-style ERC-20 transfer transaction targeting `token_id` (any account chosen at signing time, so it can be an attacker-deployed NEP-141 contract).
2. `inner_rlp_execute` builds a promise chain that first calls `token_id::storage_balance_of` and schedules `nep_141_storage_balance_callback` as the `.then()` continuation, with `has_in_flight_tx` set to `true` before returning [4](#0-3) [5](#0-4) .
3. `nep_141_storage_balance_callback` sets `self.has_in_flight_tx = false;` as its very first statement, then deserializes the promise result with `serde_json::from_slice::<Option<StorageBalance>>(&value)` [6](#0-5) .
4. Because the token contract is attacker-controlled, `storage_balance_of` can return up to `max_length_returned_data` = 4 MiB of attacker-chosen bytes (confirmed in `core/parameters/res/runtime_configs/parameters.yaml:278`). Parsing/allocating that payload consumes gas from the callback's budget, which is statically capped at `NEP_141_STORAGE_BALANCE_CALLBACK_GAS` (15 Tgas) plus the *user-signed* `action.gas()` [7](#0-6) [8](#0-7) . Any modest `action.gas()` value chosen by the honest owner (a normal amount for a token transfer, not an unusually large one) can be exhausted by a large enough attacker-crafted response, causing the callback's execution to trap with an out-of-gas error.
5. In NEAR, a receipt that traps discards all pending state changes for that execution — including the `has_in_flight_tx = false` write made earlier in the same function — so the flag remains `true` in the persisted account state.
6. There is no other method in the contract that can reset `has_in_flight_tx`; `rlp_execute` immediately short-circuits with an error whenever it's `true` [9](#0-8) .

The result: an attacker who merely needs to be named as the transfer target of one transaction (no privileged relayer/access-key role required) can permanently disable the wallet-contract account.

### Impact Explanation
Once `has_in_flight_tx` is stuck `true`, the owner can never submit another `rlp_execute` transaction through this account. Since this is the wallet contract's *only* entry point for moving assets (NEAR transfers, NEP-141 token transfers, key management all flow through `rlp_execute`/`action_to_promise`), all NEAR tokens and NEP-141/other token balances associated with that eth-implicit account become permanently inaccessible. This is a **permanent freezing of funds** for the affected user, triggerable by an unprivileged third party (the operator of the target token contract) using only a standard, valid signed transaction.

### Likelihood Explanation
Likelihood is moderate-to-high in adversarial conditions: an attacker who deploys or controls a NEP-141-like contract and gets a user to interact with it (a very common phishing/rug pattern for token transfers) can trigger this on demand by returning an oversized `storage_balance_of` response. No special privileges, races, or validator collusion are required — only that the wallet owner signs a transaction whose `to` address resolves to the attacker's contract.

### Recommendation
- Bound the size of cross-contract call results processed by wallet-contract callbacks (e.g., reject/short-circuit on oversized `promise_result` payloads before attempting to deserialize, using `env::promise_result` size introspection where available).
- Reserve callback gas independent of, and unaffected by, attacker-controlled response size, or perform deserialization with an explicit gas/size guard so a trap cannot occur after `has_in_flight_tx` has been logically cleared.
- Add a recovery path (e.g., an owner-privileged or full-access-key `force_reset_in_flight` method, or a timeout-based automatic clear) so a stuck `has_in_flight_tx` cannot permanently brick the account.
- Consider persisting `has_in_flight_tx = false` via a low-level, gas-cheap host call (e.g., direct storage write) executed before any untrusted-data-dependent logic, rather than relying on it being included in the same all-or-nothing state commit as the rest of the callback.

### Proof of Concept
1. Deploy a malicious "NEP-141-like" contract at `token.attacker.near` whose `storage_balance_of` method returns a JSON array/string close to the `max_arguments_length`/`max_length_returned_data` limit (4 MiB) of attacker-controlled bytes structured to maximize `serde_json` parsing cost (e.g., deeply nested or extremely long strings within the size budget).
2. Have the wallet owner (or trick them into) sign and submit, via a relayer, an RLP-encoded ERC-20 `transfer` transaction with `to = token.attacker.near` through `rlp_execute` (as in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs`) with a typical/legitimate `gas` value.
3. `inner_rlp_execute` schedules the `storage_balance_of` call and `nep_141_storage_balance_callback` with `has_in_flight_tx = true`.
4. The malicious `storage_balance_of` returns its oversized payload; `nep_141_storage_balance_callback` sets `has_in_flight_tx = false` then traps while deserializing the payload, exceeding its allotted gas.
5. Because the callback receipt failed, the state write is rolled back; the persisted `has_in_flight_tx` remains `true`.
6. Any subsequent `rlp_execute` call by the legitimate owner now immediately returns `"Error: transaction already in progress, please try again later."` forever, as shown by the existing guard [10](#0-9) , permanently freezing the account's NEAR balance and any other assets.

**Uncertainty**: I could not directly execute this against a live/test node to confirm the precise gas arithmetic (i.e., whether a "typical" `action.gas()` value is always insufficient to safely absorb a 4 MiB `serde_json::from_slice`, versus only unusually low ones). This would need empirical gas-profiling in a sandboxed test (e.g., extending `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs`) to fully confirm exploitability thresholds, but the code path and rollback-on-trap semantics that create the freeze condition are directly supported by the cited source.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L37-41)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L89-106)
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
        let current_account_id = env::current_account_id();
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L115-128)
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
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L195-221)
```rust
    pub fn nep_141_storage_balance_callback(
        &mut self,
        token_id: AccountId,
        receiver_id: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
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
