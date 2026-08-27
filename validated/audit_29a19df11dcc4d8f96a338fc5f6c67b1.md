### Title
Attached NEAR value silently dropped when an eth-emulated `AddKey`/`DeleteKey` transaction is combined with a nonzero `tx.value` - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs])

### Summary
The `WalletContract::rlp_execute` entry point shares a single code path (`internal::parse_rlp_tx_to_action`) to turn any Ethereum-style transaction into a Near action, regardless of which selector (`FunctionCall`, `Transfer`, `AddKey`, `DeleteKey`, or an eth-emulation fallback) is decoded. This mirrors the Sherlock finding's root cause: a shared caller/dispatcher assumes a value-like field is always consumable by every branch, but some branches never implemented handling for it.

### Finding Description
`parse_rlp_tx_to_action` always computes `tx.value` (the Ethereum "value" field, representing attached $NEAR) and unconditionally forwards it as `additional_value` into `Action::try_into_near_action`: [1](#0-0) 

`try_into_near_action` only adds `additional_value` into the resulting Near action's `deposit` for the `FunctionCall` and `Transfer` branches. For `AddKey` and `DeleteKey`, `additional_value` is simply never referenced, so any nonzero value is discarded: [2](#0-1) 

The `AddKey`/`DeleteKey` selectors are reached via `parse_tx_data`, using the exact same shared dispatch as `FunctionCall`/`Transfer`, with no validation that `tx.value == 0` for these two selectors: [3](#0-2) 

Only `validate_tx_value` bounds-checks the magnitude of `tx.value` against `VALUE_MAX`; it never rejects a nonzero value for action kinds that cannot carry it: [4](#0-3) 

Because the relayer/predecessor attaches real NEAR balance to `rlp_execute` (a `#[payable]` method) matching the fee/value the user intends, and the resulting `AddKey`/`DeleteKey` promises created in `action_to_promise` carry no deposit at all, any $NEAR value encoded in the signed Ethereum transaction's `value` field for an `AddKey`/`DeleteKey` transaction has no effect on the resulting action — it is neither transferred to the target nor refunded to the relayer/caller through the normal action execution path: [5](#0-4) 

This is the direct structural analog of the external report: `UniV2Adapter`/`UniV3Adapter`/`ZeroExAdapter` share dispatch logic with `BalancerV2Adapter`/`CurveAdapter` that supports a nonzero `msgValue`, but the former adapters never validate or consume that value, producing unexpected behavior. Here, `AddKey`/`DeleteKey` share the same `parse_rlp_tx_to_action` → `try_into_near_action` pipeline as `FunctionCall`/`Transfer` (which do support `tx.value`), but never consume it, silently losing it instead.

### Impact Explanation
If a user (or a relayer acting on the user's behalf) signs a valid `AddKey`/`DeleteKey` Ethereum-style transaction with a nonzero `value` field (which passes `validate_tx_value` as long as it is below `VALUE_MAX`), and the caller attaches real NEAR corresponding to that value when calling `rlp_execute`/`rlp_execute_from`, that attached value is not applied to any action and not tracked as a `CallerDeposit` refund path either (since `CallerDeposit::new` only tracks `context.attached_deposit`, and the discrepancy here is between the intended `tx.value` and what the executed action actually consumes) — funds attached to cover the user's declared value are permanently unaccounted for in the resulting on-chain action. This is a fund-loss class bug (permanent freezing/misallocation of attached NEAR), matching "concrete theft or permanent freezing of funds" in the validation criteria.

### Likelihood Explanation
Likelihood is limited by the fact that `AddKey`/`DeleteKey` are typically used with `yocto_near: 0`-style transactions in normal wallet usage, since these are self-directed key-management actions with no natural reason to carry a NEAR value. However, nothing in the code enforces `tx.value == 0` for these two selectors, so a malformed transaction (whether crafted by a buggy client, a malicious relayer front-end, or user error) reliably reproduces silent value loss, and the contract provides no error/refund signal distinguishing this case from a normal zero-value `AddKey`/`DeleteKey` transaction.

### Recommendation
In `parse_tx_data` (or in `validate_tx_value`), reject any `tx.value != 0` when the decoded selector is `ADD_KEY_SELECTOR` or `DELETE_KEY_SELECTOR` (i.e., whenever `ParsableTransactionKind::SelfNearNativeAction` results in `Action::AddKey`/`Action::DeleteKey`), returning a dedicated `UserError` (e.g. `UnexpectedValueForAction`) instead of silently proceeding. Alternatively, make `try_into_near_action` explicitly error out if `additional_value != 0` is passed for the `AddKey`/`DeleteKey` arms so mistakes are impossible regardless of caller validation.

### Proof of Concept
1. Construct a signed `NormalizedEthTransaction` whose `data` starts with `ADD_KEY_SELECTOR` and ABI-encodes a valid `AddKey` payload (as in `types::ADD_KEY_SIGNATURE`), but set `tx.value` to a nonzero amount below `VALUE_MAX` (e.g., 1 NEAR worth of wei).
2. Call `rlp_execute`/`rlp_execute_from`, attaching NEAR deposit corresponding to `tx.value` (as an honest relayer flow would, mirroring `test_caller_refunds` in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs`).
3. Trace through `parse_rlp_tx_to_action` → `parse_tx_data` (ADD_KEY_SELECTOR branch, `ParsableTransactionKind::SelfNearNativeAction`) → `validate_tx_value` (passes since value < `VALUE_MAX`) → `Action::AddKey{...}.try_into_near_action(tx.value...)`.
4. Observe that `try_into_near_action`'s `AddKey` arm ignores `additional_value`; the resulting `near_action::Action::AddKey` and subsequent `Promise::new(target).add_access_key_allowance_with_nonce(...)` in `action_to_promise` carry zero deposit. The attached NEAR corresponding to `tx.value` is not transferred to `target`, not returned to the caller, and not reflected anywhere in the emitted `ExecuteResponse`.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L159-165)
```rust
    validate_tx_value(&tx)?;

    // Call to `low_u128` here is safe because of the validation done in `validate_tx_value`
    let near_action = action
        .try_into_near_action(tx.value.raw().low_u128().saturating_mul(MAX_YOCTO_NEAR.into()))?;

    Ok((near_action, transaction_kind))
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L272-304)
```rust
        ADD_KEY_SELECTOR => {
            let (
                public_key_kind,
                public_key,
                nonce,
                is_full_access,
                is_limited_allowance,
                allowance,
                receiver_id,
                method_names,
            ) = ethabi_utils::abi_decode(&ADD_KEY_SIGNATURE, &tx.data[4..])?;
            Ok((
                Action::AddKey {
                    public_key_kind,
                    public_key,
                    nonce,
                    is_full_access,
                    is_limited_allowance,
                    allowance,
                    receiver_id,
                    method_names,
                },
                ParsableTransactionKind::SelfNearNativeAction,
            ))
        }
        DELETE_KEY_SELECTOR => {
            let (public_key_kind, public_key) =
                ethabi_utils::abi_decode(&DELETE_KEY_SIGNATURE, &tx.data[4..])?;
            Ok((
                Action::DeleteKey { public_key_kind, public_key },
                ParsableTransactionKind::SelfNearNativeAction,
            ))
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L370-376)
```rust
fn validate_tx_value(tx: &NormalizedEthTransaction) -> Result<(), Error> {
    if tx.value.raw() > VALUE_MAX {
        return Err(Error::User(UserError::ValueTooLarge));
    }

    Ok(())
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L483-501)
```rust

```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L238-299)
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
            Action::AddKey {
                public_key_kind,
                public_key,
                nonce,
                is_full_access,
                is_limited_allowance,
                allowance,
                receiver_id,
                method_names,
            } => {
                let public_key = construct_public_key(public_key_kind, &public_key)?;
                let access_key = if is_full_access {
                    AccessKey { nonce, permission: AccessKeyPermission::FullAccess }
                } else {
                    let allowance = if is_limited_allowance { Some(allowance) } else { None };
                    AccessKey {
                        nonce,
                        permission: AccessKeyPermission::FunctionCall(FunctionCallPermission {
                            allowance: allowance.map(NearToken::from_yoctonear),
                            receiver_id: receiver_id
                                .parse()
                                .map_err(|_| Error::User(UserError::InvalidAccessKeyAccountId))?,
                            method_names,
                        }),
                    }
                };
                let action = AddKeyAction { public_key, access_key };
                near_action::Action::AddKey(action)
            }
            Action::DeleteKey { public_key_kind, public_key } => {
                let action = DeleteKeyAction {
                    public_key: construct_public_key(public_key_kind, &public_key)?,
                };
                near_action::Action::DeleteKey(action)
            }
        };
        Ok(action)
    }
```
