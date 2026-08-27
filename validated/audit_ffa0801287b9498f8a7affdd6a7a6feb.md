## Valid Analog Found

### Title
Attached deposit is permanently lost when the intermediate NEP-141 `storage_balance_of` (or address-registrar) cross-contract call fails in `WalletContract::rlp_execute` - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Sherlock report describes `BondBaseTeller.purchase` reverting for tokens that don't expose the exact `safeTransfer`/`safeTransferFrom` signature used, because the multi-step token-interaction flow assumes standard-conforming behavior of an externally-controlled contract. The closest analog in nearcore is the eth-implicit `WalletContract`'s emulated ERC-20 transfer path, which similarly assumes the target NEP-141 token contract behaves in a fully standard way across a multi-step cross-contract call chain, and mishandles the case where an intermediate call to the token/registrar fails.

### Finding Description
`inner_rlp_execute` handles `EthEmulationKind::ERC20Transfer` by first calling `storage_balance_of` on the target token contract and chaining to `nep_141_storage_balance_callback`, immediately after which (before knowing whether that call even succeeds) it also creates a promise transferring the relayer `fee` out of the predecessor's attached deposit: [1](#0-0) .

The attached deposit of the *external* caller (the value sent along with the `rlp_execute` call) is tracked via `CallerDeposit`, specifically so it can be refunded if a cross-contract call later fails: [2](#0-1) .

However, that refund logic is only implemented in the final `rlp_execute_callback`: [3](#0-2) .

The intermediate callbacks that sit in front of it — `nep_141_storage_balance_callback` (triggered after calling the token's `storage_balance_of`) and `address_check_callback` (triggered after calling the address registrar) — do **not** perform this refund when their respective cross-contract call fails; they simply reset `has_in_flight_tx` and return a failure `ExecuteResponse`: [4](#0-3) [5](#0-4) 

Because `storage_balance_of` is called on a token contract that is not controlled by nearcore itself (any NEP-141-labelled account can be `target`), any token that reverts, panics, runs out of gas, or simply does not implement `storage_balance_of` in the exact way NEP-145 expects will cause `PromiseResult::Failed` in `nep_141_storage_balance_callback`. In that branch the caller's attached NEAR deposit is never returned to `context.predecessor_account_id`, unlike the symmetric failure path in `rlp_execute_callback`.

### Impact Explanation
Any relayer or EOA-owner submitting an ERC20-emulated transfer (`EthEmulationKind::ERC20Transfer`) with a nonzero attached deposit against a non-standard-conforming or misbehaving NEP-141 token contract will have that deposit permanently stuck in the wallet contract: it is neither forwarded to the token/registrar contract (since that call itself failed) nor refunded to the original sender. This is a permanent freezing-of-funds condition reachable from an ordinary signed transaction routed through the eth-implicit wallet contract, without requiring any privileged role.

### Likelihood Explanation
Reachable whenever an eth-implicit account holder or their relayer sends an ERC-20-style emulated transfer with an attached deposit to a token whose `storage_balance_of` call can fail — e.g., insufficient forwarded gas (`NEP_141_STORAGE_BALANCE_OF_GAS` is a fixed 5 Tgas budget), a token contract that panics, or one that does not implement NEP-145's `storage_balance_of` exactly. This does not require the token owner to be malicious; ordinary edge cases (gas exhaustion, non-standard tokens) trigger it, similar in spirit to the original report where any non-conforming token consistently breaks the flow.

### Recommendation
In `nep_141_storage_balance_callback` (and similarly in `address_check_callback`), on `PromiseResult::Failed`, issue the same refund-to-`caller_deposit.account_id` promise that `rlp_execute_callback` performs before returning the failure `ExecuteResponse`, so that a failed intermediate cross-contract call cannot strand the caller's attached deposit inside the wallet contract.

### Proof of Concept
1. Deploy an eth-implicit `WalletContract` and an NEP-141-labelled `target` token contract that either does not implement `storage_balance_of` or reverts/runs out of gas when called with the 5 Tgas budget (`NEP_141_STORAGE_BALANCE_OF_GAS`).
2. Sign an Ethereum-style transaction encoding an ERC-20 `transfer` (parsed into `EthEmulationKind::ERC20Transfer`) with a nonzero attached NEAR deposit and call `rlp_execute` via a relayer.
3. `inner_rlp_execute` schedules `storage_balance_of` on the token, then chains `nep_141_storage_balance_callback` (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:433-458`).
4. Because the token call fails, `nep_141_storage_balance_callback` hits the `PromiseResult::Failed` branch (`lib.rs:203-210`) and returns `success: false` without creating any refund promise for `caller_deposit`.
5. The attached deposit remains held by the wallet contract's account balance and is never returned to the original sender — a permanent loss of funds for the caller.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L133-159)
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L194-221)
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-312)
```rust
        match env::promise_result(0) {
            PromiseResult::Failed => {
                // The cross-contract call failed, refund the caller if needed
                if let Some(CallerDeposit { account_id, yocto_near }) = caller_deposit {
                    let refund_promise = env::promise_batch_create(&account_id);
                    env::promise_batch_action_transfer(
                        refund_promise,
                        NearToken::from_yoctonear(yocto_near.into()),
                    );
                }

                ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Failed Near promise".into()),
                }
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L374-385)
```rust
            if let TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                fee,
                ..
            })
            | TransactionKind::EthEmulation(EthEmulationKind::ERC20Transfer { fee, .. }) =
                &transaction_kind
            {
                if !fee.is_zero() && context.predecessor_account_id != context.current_account_id {
                    let refund_promise = env::promise_batch_create(&context.predecessor_account_id);
                    env::promise_batch_action_transfer(refund_promise, *fee);
                }
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L172-191)
```rust
/// A data type to keep track of the deposit given by an external caller.
/// This allows us to refund the caller's deposit if the cross-contract call fails.
#[derive(Debug, PartialEq, Eq, Clone, serde::Serialize, serde::Deserialize)]
pub struct CallerDeposit {
    pub account_id: AccountId,
    pub yocto_near: NonZeroU128,
}

impl CallerDeposit {
    pub fn new(context: &ExecutionContext) -> Option<Self> {
        // Only track for external (non-self) callers
        if context.current_account_id == context.predecessor_account_id {
            return None;
        }

        NonZeroU128::new(context.attached_deposit.as_yoctonear()).map(|yocto_near| Self {
            account_id: context.predecessor_account_id.clone(),
            yocto_near,
        })
    }
```
