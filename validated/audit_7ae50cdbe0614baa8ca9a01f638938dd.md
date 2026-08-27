## Finding

### Title
Attached deposit permanently lost when the address-registrar lookup or NEP-141 `storage_balance_of` cross-contract call fails - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
The eth-implicit wallet contract tracks an external caller's attached deposit via `CallerDeposit` specifically so it can be refunded if a downstream cross-contract call fails [1](#0-0) . This refund is correctly performed in `rlp_execute_callback`'s `PromiseResult::Failed` branch [2](#0-1) , but the same refund logic is missing in two earlier callback stages — `address_check_callback` and `nep_141_storage_balance_callback` — when their respective intermediate promise fails.

### Finding Description
`inner_rlp_execute` constructs a `caller_deposit` for any non-self relayer call carrying attached NEAR [3](#0-2) , and threads it through the promise chain so the ultimate `rlp_execute_callback` can refund the caller if the final action fails.

For two transaction kinds this promise chain has an intermediate step before reaching `rlp_execute_callback`:

- `EOABaseTokenTransfer` with `address_check: Some(_)` first calls the address registrar, then continues in `address_check_callback` [4](#0-3) .
- `ERC20Transfer` first calls `storage_balance_of` on the token contract, then continues in `nep_141_storage_balance_callback` [5](#0-4) .

In both intermediate callbacks, the `PromiseResult::Failed` branch returns an error `ExecuteResponse` immediately, dropping `caller_deposit` without ever refunding it:

```rust
// address_check_callback
PromiseResult::Failed => {
    return PromiseOrValue::Value(ExecuteResponse {
        success: false,
        success_value: None,
        error: Some("Call to Address Registrar contract failed".into()),
    });
}
``` [6](#0-5) 

```rust
// nep_141_storage_balance_callback
PromiseResult::Failed => {
    return PromiseOrValue::Value(ExecuteResponse {
        success: false,
        success_value: None,
        error: Some(format!("Call to NEP-141 {token_id}::storage_balance_of failed")),
    });
}
``` [7](#0-6) 

This is the same bug class described in the external report: state that must be restored/executed on a downstream call failure (there: `split_owners`/`approvals`; here: the refund of `caller_deposit`) is silently dropped on some — but not all — failure paths, because the "rollback" step was only implemented at the final callback and forgotten in the intermediate ones.

Once `caller_deposit` is dropped this way, `self.has_in_flight_tx` is reset to `false` (line 140 / 202) and no other promise is created that references the deposit again. The attached yoctoNEAR remains held by the wallet contract's balance with no on-chain record connecting it back to the original caller (`CallerDeposit.account_id`), and the caller has no way to reclaim it.

### Impact Explanation
Any NEAR attached by an external relayer to `rlp_execute` for an `EOABaseTokenTransfer` targeting another eth-implicit account (triggering the registrar check) or for an `ERC20Transfer` (triggering the `storage_balance_of` check) is permanently frozen in the wallet contract if the intermediate cross-contract call fails (e.g., the registrar contract is paused/out of gas, or the token contract's `storage_balance_of` fails). This is a permanent freezing-of-funds bug reachable by an ordinary relayer/caller through the documented public entry point `rlp_execute`, with no privileged access required.

### Likelihood Explanation
The failure condition only requires an external cross-contract call (address registrar lookup, or a token's `storage_balance_of`) to fail — something outside the wallet contract's control that can occur due to normal external conditions (target contract paused, insufficient forwarded gas, target account deleted, etc.). Because relayers routinely attach a fee deposit for these emulated-ETH transaction kinds, this is easily triggerable in normal operation, not just a contrived edge case.

### Recommendation
In both `address_check_callback` and `nep_141_storage_balance_callback`, mirror the refund logic already present in `rlp_execute_callback`'s `Failed` branch: when the intermediate promise fails, issue a `promise_batch_action_transfer` to `caller_deposit.account_id` for `caller_deposit.yocto_near` before returning the failed `ExecuteResponse`.

### Proof of Concept
1. A relayer (predecessor ≠ current wallet account) calls `rlp_execute` with an RLP transaction that is an `EOABaseTokenTransfer` targeting another eth-implicit account, attaching deposit `D` yoctoNEAR as `caller_deposit`.
2. `inner_rlp_execute` schedules a lookup to the address registrar, chained to `address_check_callback` with `caller_deposit = Some(CallerDeposit { account_id: relayer, yocto_near: D })` [4](#0-3) .
3. The address registrar's `lookup` promise fails (e.g., insufficient `REGISTRAR_LOOKUP_GAS`, or the registrar contract is paused).
4. `address_check_callback` observes `PromiseResult::Failed`, sets `has_in_flight_tx = false`, and returns a failed `ExecuteResponse` without ever creating a transfer promise back to the relayer [6](#0-5) .
5. The `D` yoctoNEAR remains in the wallet contract's account balance permanently; the relayer has no mechanism to reclaim it. The same scenario applies to `nep_141_storage_balance_callback` when the token's `storage_balance_of` call fails.

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L140-148)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L203-210)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L340-345)
```rust
    let context = ExecutionContext::new(
        current_account_id.clone(),
        predecessor_account_id,
        env::attached_deposit(),
    )?;
    let caller_deposit = CallerDeposit::new(&context);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L412-431)
```rust
    let promise = match transaction_kind {
        TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
            address_check: Some(address),
            ..
        }) => {
            let callback_gas = ADDRESS_CHECK_CALLBACK_GAS.saturating_add(action.gas());
            let ext = WalletContract::ext(current_account_id).with_static_gas(callback_gas);
            let address_registrar = {
                let account_id = ADDRESS_REGISTRAR_ACCOUNT_ID
                    .trim()
                    .parse()
                    .unwrap_or_else(|_| env::panic_str("Invalid address registrar"));
                ext_registrar::ext(account_id).with_static_gas(REGISTRAR_LOOKUP_GAS)
            };
            let address = format!("0x{}", hex::encode(address));
            address_registrar.lookup(address).then(ext.address_check_callback(
                target,
                action,
                caller_deposit,
            ))
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
