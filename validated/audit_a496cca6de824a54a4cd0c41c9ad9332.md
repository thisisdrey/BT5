Based on my research, I found a strong analog to the "ETH dust" pattern in the `near-wallet-contract` (the ETH-implicit account contract that emulates Ethereum-style transactions on NEAR).

### Title
Wallet Contract's `rlp_execute` Traps Unspent Attached NEAR Deposit On Successful Execution - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
`WalletContract::rlp_execute` is a `#[payable]` entry point that accepts an attached NEAR deposit from an external caller/relayer [1](#0-0) . This mirrors the reported `createAndInitializePool` bug class: a payable function that accepts value but only has a refund path for one branch (failure), not for the general "unused/leftover deposit" case.

### Finding Description
`inner_rlp_execute` builds a `CallerDeposit` from the attached deposit only for external (non-self) callers, and this deposit is refunded to the caller **only if the resulting promise chain fails** [2](#0-1) [3](#0-2) . On success, no refund logic exists at all — the code simply reports success without checking whether the attached deposit was fully consumed by the constructed NEAR action [4](#0-3) .

The value actually forwarded into the resulting `near_action::Action` comes from `Action::try_into_near_action`, which combines a small ABI-encoded `yocto_near: u32` field with an `additional_value: u128` parameter [5](#0-4) . I was not able to fully trace `internal::parse_rlp_tx_to_action` (the function that computes `additional_value` from the attached deposit for each transaction kind) in the time available, so I cannot conclusively prove that `additional_value` always equals the full `attached_deposit` for every `TransactionKind` branch (native actions vs. Ethereum-emulation branches such as ERC-20 transfer/self-transfer). If any code path constructs an action whose deposit is less than the attached deposit — while the promise still succeeds — the surplus becomes untracked and is never refunded, permanently increasing the wallet contract's own NEAR balance instead of returning it to the caller.

### Impact Explanation
If a code path exists (e.g. certain `EthEmulationKind` variants, where `yocto_near` is a truncated `u32` and the actual transfer amount is intentionally routed elsewhere, such as the `fee` sent to the relayer) where the deposit forwarded into the on-chain action is less than the deposit attached by the caller, and the resulting promise succeeds, then the difference is silently absorbed by the wallet contract account rather than refunded — a permanent loss of funds for the relayer/caller, analogous to ETH being trapped in `createAndInitializePool`.

### Likelihood Explanation
Likelihood cannot be assessed with high confidence without reading `internal::parse_rlp_tx_to_action`, which determines whether `additional_value` (and therefore the forwarded action deposit) is always kept equal to `attached_deposit`. The refund-on-failure-only design pattern in `rlp_execute_callback` is confirmed and is architecturally the same "no general refund path" gap described in the report, but I could not confirm a concrete forwarding-amount mismatch in this pass. Because the funds involved would be actual NEAR deposits made by contract callers/relayers, if a mismatch exists it would be directly exploitable/lossy, not just theoretical.

### Recommendation
Audit `internal::parse_rlp_tx_to_action` and every `TransactionKind`/`EthEmulationKind` branch in `inner_rlp_execute` to confirm the deposit forwarded to the constructed NEAR action (plus any relayer `fee` transfer) always equals the full attached deposit. If any branch can leave a remainder, add an explicit refund of the leftover deposit back to `predecessor_account_id` in the success path of `rlp_execute_callback`, not just the failure path.

### Proof of Concept
Not constructed — this requires confirming, via `internal::parse_rlp_tx_to_action`, a concrete transaction encoding where `additional_value + yocto_near < attached_deposit` for a branch that still resolves via `PromiseResult::Successful`. I was unable to complete this trace within the available tool budget; a Devin session with full repository access should read `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs` to close this gap before treating the finding as confirmed rather than a strong analog candidate.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-93)
```rust
    #[payable]
    pub fn rlp_execute(
        &mut self,
        target: AccountId,
        tx_bytes_b64: String,
    ) -> PromiseOrValue<ExecuteResponse> {
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L313-316)
```rust
            PromiseResult::Successful(value) => {
                ExecuteResponse { success: true, success_value: Some(value), error: None }
            }
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L172-192)
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
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L238-261)
```rust
    pub fn try_into_near_action(
        self,
        additional_value: u128,
    ) -> Result<near_action::Action, Error> {
        let action = match self {
            Action::FunctionCall { receiver_id: _, method_name, args, gas, yocto_near } => {
                let action = FunctionCallAction {
                    method_name,
                    args,
                    gas: Gas::from_gas(gas),
                    deposit: NearToken::from_yoctonear(
                        additional_value.saturating_add(yocto_near.into()),
                    ),
                };
                near_action::Action::FunctionCall(action)
            }
            Action::Transfer { receiver_id: _, yocto_near } => {
                let action = TransferAction {
                    deposit: NearToken::from_yoctonear(
                        additional_value.saturating_add(yocto_near.into()),
                    ),
                };
                near_action::Action::Transfer(action)
            }
```
