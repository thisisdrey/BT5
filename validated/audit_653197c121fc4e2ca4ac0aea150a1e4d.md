## Title
Attached deposit is permanently lost when the address-registrar lookup or NEP-141 `storage_balance_of` pre-check fails in the Wallet Contract - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Wallet Contract (NEP-518 eth-implicit account contract) is designed to refund an external caller's attached NEAR deposit whenever a cross-contract call it schedules on the caller's behalf fails, as documented on `CallerDeposit`. This refund is correctly implemented in the final `rlp_execute_callback`, but it is missing in two of the three failure branches that consume a `PromiseResult::Failed` — `address_check_callback` and `nep_141_storage_balance_callback`. If the pre-check cross-contract call in either of these paths fails, the caller's attached deposit is silently kept by the wallet contract instead of being returned, a permanent loss of the caller's funds.

### Finding Description
`CallerDeposit` is created in `inner_rlp_execute` specifically to track a deposit attached by an *external* caller (`predecessor_account_id != current_account_id`) so it can be refunded if the resulting cross-contract call fails: [1](#0-0) 

The final callback that resolves an actual action promise correctly refunds this deposit on failure: [2](#0-1) 

However, two earlier callbacks in the promise chain — which are used whenever the target of the transaction is another eth-implicit account (requiring an address-registrar lookup) or an unregistered NEP-141 receiver (requiring a `storage_balance_of` check) — receive the `caller_deposit` and simply drop it on failure, with no refund logic at all:

`address_check_callback` (registrar lookup path): [3](#0-2) 

`nep_141_storage_balance_callback` (NEP-141 storage check path): [4](#0-3) 

In both cases, `env::promise_result(0)` returning `PromiseResult::Failed` (i.e. the cross-contract call to the registrar contract or to the target NEP-141 token's `storage_balance_of` failed) is handled by returning an `ExecuteResponse{success: false, ...}` directly, with no `env::promise_batch_create` / `promise_batch_action_transfer` back to `caller_deposit.account_id`, unlike the pattern used in `rlp_execute_callback`. Because the attached deposit for a `#[payable]` method call is already credited to the contract's own account balance as part of receipt processing, failing to explicitly forward it back means it remains stuck in the eth-implicit account's balance rather than being returned to the depositor.

These two calls use small, fixed gas budgets (`REGISTRAR_LOOKUP_GAS` and `NEP_141_STORAGE_BALANCE_OF_GAS`, both `Gas::from_tgas(5)`), so an ordinary (non-malicious) registrar or token implementation that is slightly heavier, or that panics/is missing the expected view method, can easily cause `PromiseResult::Failed` here in normal operation, not just via an adversarial contract: [5](#0-4) 

### Impact Explanation
Any external account that calls `rlp_execute` on someone's eth-implicit Wallet Contract while attaching a NEAR deposit (a use case the contract explicitly supports via `CallerDeposit`) permanently loses that deposit if the transaction is either (a) an `EOABaseTokenTransfer`/`ERC20Transfer` to a target resolved as another eth-implicit account requiring an address-registrar check, or (b) an `ERC20Transfer` to an unregistered NEP-141 receiver requiring a `storage_balance_of` check — and the corresponding pre-check call fails. This is a permanent loss of user funds for the depositor (the funds become indistinguishable additions to the wallet's own account balance, benefiting the wallet owner instead), directly analogous to the reported bug class of not properly handling a downstream call/transfer failure across a trust boundary.

### Likelihood Explanation
This is reachable by any ordinary account interacting with a Wallet Contract instance via `rlp_execute`, requiring no validator/relayer privilege. The two vulnerable code paths are exercised by normal Ethereum-emulated transactions targeting other eth-implicit accounts or unregistered NEP-141 receivers — both common flows for this contract — and the tight 5 Tgas budgets on the underlying cross-contract calls make transient failures plausible in ordinary operation, not just adversarial ones.

### Recommendation
Mirror the refund logic already present in `rlp_execute_callback`: in the `PromiseResult::Failed` branches of both `address_check_callback` and `nep_141_storage_balance_callback`, if `caller_deposit` is `Some`, issue a `promise_batch_create` + `promise_batch_action_transfer` back to `caller_deposit.account_id` for `caller_deposit.yocto_near` before returning the failure `ExecuteResponse`.

### Proof of Concept
1. Account `A` (external, `A != wallet_account_id`) calls `wallet_contract.rlp_execute(target, tx_bytes_b64)` with an attached deposit of e.g. 1 NEAR, where the RLP-encoded transaction is a base token transfer whose `target` resolves to `TargetKind::EthImplicit(address)` (per `parse_target`), OR an ERC-20 transfer to an unregistered receiver.
2. `inner_rlp_execute` computes `caller_deposit = Some({account_id: A, yocto_near: 1 NEAR})` since predecessor ≠ current account.
3. The scheduled `address_registrar.lookup(...)` (case 1) or `token_id::storage_balance_of(...)` (case 2) call fails (`PromiseResult::Failed`) — e.g. due to the 5 Tgas budget being insufficient or the target contract lacking/panicking on the method.
4. `address_check_callback`/`nep_141_storage_balance_callback` executes its `PromiseResult::Failed` arm and returns `ExecuteResponse{success:false,...}` without transferring anything back to `A`.
5. `A`'s attached 1 NEAR deposit remains permanently in the wallet contract's own balance; `A` receives no refund, in contrast to the guarantee documented for `CallerDeposit` and implemented correctly in `rlp_execute_callback`.

### Citations

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L134-148)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L194-210)
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
