### Title
AddKey/DeleteKey actions bypass self-target invariant and can be executed against an arbitrary victim account - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs])

### Finding Description
`parse_tx_data`'s `ADD_KEY_SELECTOR`/`DELETE_KEY_SELECTOR` branches decode `Action::AddKey`/`Action::DeleteKey` from the ABI payload and unconditionally return `Ok((action, ParsableTransactionKind::SelfNearNativeAction))` without checking `target` at all: [1](#0-0) 

The comment on `ParsableTransactionKind::SelfNearNativeAction` states these actions are meant to only ever target `current_account_id`: [2](#0-1) 

However, the only place that enforces this invariant is in `parse_rlp_tx_to_action`, and it only special-cases `TargetKind::EthImplicit`: [3](#0-2) 

For `TargetKind::CurrentAccount` and `TargetKind::OtherNearAccount(_)`, the `else` branch executes and simply returns `(action, TransactionKind::NearNativeAction)` verbatim — no assertion that `target == context.current_account_id`. This means an attacker relayer can set `target` to any existing named account, as long as `to == account_id_to_address(target)` is satisfied in `validate_tx_relayer_data` (a check the attacker trivially controls by hashing the chosen victim account themselves): [4](#0-3) 

Once the action and `TransactionKind::NearNativeAction` reach `inner_rlp_execute`, the default match arm builds the promise using `target` directly, not `current_account_id`: [5](#0-4) 

`action_to_promise` then creates `Promise::new(target)` carrying the `AddKey`/`DeleteKey` action: [6](#0-5) 

`FullAccess` key additions are explicitly rejected (`UnsupportedAction::AddFullAccessKey`), but `FunctionCall`-permission `AddKey` and `DeleteKey` are not rejected, so they proceed to the receiver (`target`) account as-is.

### Impact Explanation
Because NEAR's receipt-level `AddKey`/`DeleteKey` actions execute on whatever `receiver_id` the receipt names — without any ownership check on the receiving side (this is the same mechanism used by linkdrop/factory contracts to add keys to freshly created or third-party accounts) — a wallet contract owner (attacker) can craft an RLP transaction with the `AddKey` selector and set `target` to any victim's plain NEAR account. This results in a `FunctionCall`-permission access key being added to the victim's account under attacker control, or an existing key on the victim account being deleted, without ever compromising the victim's private key. This is authorization escalation across accounts: the attacker can subsequently use the newly-added function-call key to spend the victim account's allowance (draining balance via gas costs) and invoke methods on the specified `receiver_id` contract on behalf of the victim (e.g., transferring NEP-141 tokens the victim owns), or deny the victim service by deleting one of their keys.

### Likelihood Explanation
The precondition is only that the attacker controls a wallet-contract account (any user can deploy/own one as an eth-implicit account) and knows the victim's plain account ID (public information). The attacker computes `account_id_to_address(target)` themselves (a public, deterministic keccak-based hash) to satisfy `validate_tx_relayer_data`'s target check, and crafts calldata with `ADD_KEY_SELECTOR`. No special privilege, leaked key, or validator access is required — this is fully reachable by an ordinary client submitting `rlp_execute` through public RPC, and is repeatable against any target account.

### Recommendation
In `parse_rlp_tx_to_action`, when handling `Ok((action, ParsableTransactionKind::SelfNearNativeAction))`, explicitly reject (or convert to an emulated transfer only for `TargetKind::EthImplicit`, as already done) any case where `target_kind` is not `TargetKind::CurrentAccount`. Concretely, add a check in the `else` branch (or before dispatch) that returns `Err(Error::Relayer(RelayerError::InvalidTarget))` when `target != context.current_account_id`, ensuring `AddKey`/`DeleteKey` actions can only ever be promised against `current_account_id`.

### Proof of Concept
Runtime/unit-test plan (extending `internal.rs`'s existing test module or `tests/relayer.rs`):
1. Build an `ExecutionContext` for wallet account `W` (eth-implicit, address `A`).
2. Create a plain victim NEAR account ID `victim.near` (not eth-implicit).
3. Construct an RLP Ethereum transaction: `from = A`, `to = account_id_to_address(&"victim.near".parse().unwrap())`, `data = ADD_KEY_SELECTOR || abi_encode(pubkey_kind, pubkey_bytes, nonce, is_full_access=false, is_limited_allowance=true, allowance, receiver_id="some-contract.near", method_names=[])`.
4. Call `parse_rlp_tx_to_action(tx_bytes_b64, target=&"victim.near".parse().unwrap(), &context, expected_nonce)`.
5. Assert it returns `Ok((near_action::Action::AddKey(_), TransactionKind::NearNativeAction))` instead of an `Err(Error::Relayer(RelayerError::InvalidTarget))`.
6. Additionally, in an integration/runtime test (`integration-tests/src/tests/features/wallet_contract.rs`), call `rlp_execute(target=victim_account, tx_bytes_b64)` end-to-end and assert that a receipt with an `AddKey` action targeting `victim_account` is produced (rather than the call failing), confirming the key is added to an account the signer does not own.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L87-106)
```rust
        Ok((action, ParsableTransactionKind::SelfNearNativeAction)) => {
            if let TargetKind::EthImplicit(_) = target_kind {
                // The calldata was parseable as a Near native action where the target
                // should be the current account, but the target is some other wallet contract.
                // This is technically allowed under the Ethereum standard for base token transfers
                // (where any calldata can be used when sending tokens to another EOA), so we
                // assume such a transfer must have been the user's intent. No address check is
                // required in this case because no Near account other than the current account
                // can be the receiver of these actions.
                (
                    Action::Transfer { receiver_id: target.to_string(), yocto_near: 0 },
                    TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                        address_check: None,
                        fee: tx_fee,
                    }),
                )
            } else {
                (action, TransactionKind::NearNativeAction)
            }
        }
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L336-350)
```rust
    // valid targets satisfy `to == target` or `to == hash(target)`
    let is_valid_target = match target_kind {
        TargetKind::CurrentAccount if to == context.current_address => {
            target == &context.current_account_id
        }
        TargetKind::EthImplicit(address) if to == address => {
            target.as_str()
                == format!("0x{}{}", hex::encode(address), context.current_account_suffix())
        }
        _ => to == account_id_to_address(target),
    };

    if !is_valid_target {
        return Err(Error::Relayer(RelayerError::InvalidTarget));
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L144-148)
```rust
    NearNativeAction,
    /// Near native actions where the receiver should be equal
    /// to the current account (i.e. `AddKey` and `DeleteKey`).
    SelfNearNativeAction,
    /// Emulated Ethereum standards
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L466-470)
```rust
        _ => {
            let ext =
                WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
            action_to_promise(target, action)?.then(ext.rlp_execute_callback(caller_deposit))
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L475-500)
```rust
fn action_to_promise(target: AccountId, action: near_action::Action) -> Result<Promise, Error> {
    match action {
        near_action::Action::FunctionCall(action) => Ok(Promise::new(target).function_call(
            action.method_name,
            action.args,
            action.deposit,
            action.gas,
        )),
        near_action::Action::Transfer(action) => Ok(Promise::new(target).transfer(action.deposit)),
        near_action::Action::AddKey(action) => match action.access_key.permission {
            near_action::AccessKeyPermission::FullAccess => {
                Err(Error::User(UserError::UnsupportedAction(UnsupportedAction::AddFullAccessKey)))
            }
            near_action::AccessKeyPermission::FunctionCall(access) => Ok(Promise::new(target)
                .add_access_key_allowance_with_nonce(
                    action.public_key,
                    access.allowance.and_then(Allowance::limited).unwrap_or(Allowance::Unlimited),
                    access.receiver_id,
                    access.method_names.join(","),
                    action.access_key.nonce,
                )),
        },
        near_action::Action::DeleteKey(action) => {
            Ok(Promise::new(target).delete_key(action.public_key))
        }
    }
```
