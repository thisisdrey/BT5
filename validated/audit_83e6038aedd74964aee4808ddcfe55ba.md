### Title
DelegateAction replay via gas-key DeleteKey+AddKey nonce reset within the same block enables double execution of a signed meta-transaction - (File: `runtime/runtime/src/access_keys.rs`)

### Summary
The gas-key nonce-collision-avoidance scheme, which seeds a freshly (re)created gas key's nonces from `initial_nonce_value(block_height)`, is computed purely from the current block height and unconditionally overwrites *all* nonce slots on `AddKey`, without regard to nonces already consumed earlier in the same block. An account owner can use an already-consumed `DelegateAction` nonce, delete and re-add the same gas key within the same block, and have a previously executed `SignedDelegateAction`/`VersionedSignedDelegateAction` accepted again by `validate_delegate_action_key`, causing the wrapped `FunctionCall` (e.g., an `ft_transfer`/reward-claim call) to execute a second time.

### Finding Description
`initial_nonce_value` derives a gas key's baseline nonce solely from `block_height`: [1](#0-0) 

When a gas key is (re-)added via `AddKey`, `add_gas_key` unconditionally resets every nonce slot to that baseline, with no memory of nonces consumed before the delete: [2](#0-1) 

`DeleteKey`/`AddKey` may only be issued by the account owner (`actor_id == account_id`), which an ordinary, unprivileged NEAR account always satisfies for its own keys: [3](#0-2) 

`validate_delegate_action_key` accepts a `DelegateAction` whenever its nonce is strictly greater than the currently stored nonce for the selected slot and below the block-height-derived upper bound: [4](#0-3) 

Exploit flow (single account, no privileged role):
1. Alice owns a gas key (created at an earlier height `H0 < H`) used to relay `DelegateAction`s to a token/DeFi contract that has no application-level idempotency check and trusts NEAR's nonce guarantee (as documented: "Nonce to ensure that the same delegate action is not sent twice by a relayer" — see `docs/RuntimeSpec/Actions.md:352-354`).
2. At block height `H`, Alice signs `DelegateAction_1` with nonce `N = (H-1)*1_000_000 + 1` (just above the floor for height `H`, still below the upper bound `H*1_000_000`), wrapping a `FunctionCall` such as `ft_transfer`/claim-reward on the target contract. It executes successfully, storing `current_nonce = N` for that slot.
3. In the same block `H`, using her own regular full-access key, Alice submits `DeleteKey` (removing the gas key and all its nonce rows via `delete_gas_key`) followed by `AddKey` (re-adding the same public key as a gas key). `add_gas_key` resets the nonce for every slot to `initial_nonce_value(H) = (H-1)*1_000_000`, which is **less than** `N`.
4. Alice (or any relayer she gives the already-signed `SignedDelegateAction`/`VersionedSignedDelegateAction` to) resubmits the exact same, already-executed `DelegateAction_1`. `validate_delegate_action_key` sees `current_nonce = (H-1)*1_000_000 < N`, so the check `delegate_nonce.nonce() <= current_nonce` passes the replay, and the wrapped `FunctionCall` executes a second time.

No existing check stops this: signature verification passes (it's the same valid signature), the nonce check passes because the stored baseline was rolled back, and the actor-permission checks for `DeleteKey`/`AddKey` are satisfied trivially by the account owner acting on her own keys. This only works when the `DeleteKey`+`AddKey` execute at the same block height as the original `DelegateAction` execution (shown by the fact that `N < H*1_000_000 <= (H'-1)*1_000_000` for any `H' > H`), which matches the stated precondition and is easily achievable by submitting several transactions in quick succession so they land in the same block/chunk.

### Impact Explanation
Any contract that relies on NEAR's protocol-level guarantee that a signed `DelegateAction` cannot be replayed (a guarantee explicitly documented in NEP-366/`docs/RuntimeSpec/Actions.md`) can have its wrapped `FunctionCall` executed twice from a single signed payload. For contracts whose method has value-creating or value-transferring semantics not protected by a caller-balance check (e.g., a claim/withdraw from a shared pool, reward/airdrop distribution, or any operation gated only on "this signed request executes once"), this results in token duplication or loss — a genuine value-conservation violation at the contract layer, categorized under token inflation/duplication and double-spend/replay in the NEAR bounty taxonomy.

### Likelihood Explanation
The attacker needs only an ordinary funded account with a gas key relaying `DelegateAction`s (no validator/node/RPC-operator privilege). The only timing requirement is getting the original `DelegateAction` execution and the `DeleteKey`+`AddKey` transaction into the same block, which is readily achievable by submitting transactions back-to-back on a live network with normal block times, and is fully reproducible/repeatable at will by the account owner. The attacker also needs the delegate nonce to have been chosen close to the current block's floor value, which is entirely under the attacker's control since she signs the `DelegateAction` herself.

### Recommendation
Do not reset gas-key nonces purely from `block_height` on `AddKey`; instead, either (a) forbid gas-key nonce resets from going backward relative to any nonce value the same public key may have already consumed in the current or a recent block (e.g., persist a monotonically increasing "epoch"/generation counter across delete/re-add cycles rather than re-deriving from `block_height` alone), or (b) additionally seed with a component that can't collide across a delete+add within the same block, such as combining `block_height` with a global monotonically-increasing counter or the previous nonce value read before deletion.

### Proof of Concept
Test-loop integration test (extending `test-loop-tests/src/tests/gas_keys.rs`):
1. Deploy a minimal FT-like contract with a `claim_once`/`ft_transfer`-style method that mutates internal balance state without its own nonce/idempotency guard.
2. Add a gas key to `sender` (as in `test_gas_key_delegate_v2_meta_transaction`).
3. Sign `DelegateActionV2` with nonce `N = (H-1)*1_000_000 + 1` for the current height `H`, wrapping a `FunctionCall` to the FT contract's mutating method; submit via a relayer transaction and assert success and the balance mutation happened once.
4. In the same block, submit a transaction from `sender`'s own full-access key containing `Action::DeleteKey` then `Action::AddKey` for the same gas key public key (same `num_nonces`).
5. Re-submit the identical `signed_delegate` from step 3 via a (possibly different) relayer transaction in the same or next receipt window before another block passes.
6. Assert the second submission succeeds (`FinalExecutionStatus::SuccessValue`) rather than failing with `DelegateActionInvalidNonce`, and assert the FT contract's internal balance state changed twice — demonstrating value duplication despite the runtime nonce bookkeeping reporting a "valid" state.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L204-214)
```rust
    // For gas keys, nonce stored on access key is not used and should always be zero
    let mut access_key = access_key.clone();
    access_key.nonce = 0;
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);

    // Set up nonces for gas key
    let num_nonces = gas_key_info.num_nonces;
    let nonce = initial_nonce_value(block_height);
    for i in 0..num_nonces {
        set_gas_key_nonce(state_update, account_id.clone(), public_key.clone(), i, nonce);
    }
```

**File:** runtime/runtime/src/actions.rs (L589-650)
```rust
    let delegate_nonce = delegate_action.nonce();
    let (current_nonce, nonce_update) = match delegate_nonce {
        TransactionNonce::Nonce { .. } => {
            if access_key.gas_key_info().is_some() {
                result.result = Err(ActionErrorKind::DelegateActionAccessKeyError(
                    InvalidAccessKeyError::DelegateActionRequiresNonGasKey,
                )
                .into());
                return Ok(());
            }
            (access_key.nonce, DelegateNonceUpdate::AccessKey)
        }
        TransactionNonce::GasKeyNonce { nonce_index, .. } => {
            let Some(gas_key_info) = access_key.gas_key_info() else {
                result.result = Err(ActionErrorKind::DelegateActionAccessKeyError(
                    InvalidAccessKeyError::DelegateActionRequiresGasKey,
                )
                .into());
                return Ok(());
            };
            if nonce_index >= gas_key_info.num_nonces {
                result.result = Err(ActionErrorKind::DelegateActionInvalidNonceIndex {
                    nonce_index,
                    num_nonces: gas_key_info.num_nonces,
                }
                .into());
                return Ok(());
            }
            // The index is range-checked above and gas keys initialize every
            // nonce row at creation, so a missing row is inconsistent state.
            let current_nonce =
                get_gas_key_nonce(state_update, sender_id, public_key, nonce_index)?.ok_or_else(
                    || {
                        StorageError::StorageInconsistentState(format!(
                            "gas key nonce row missing for {} {} at in-range index {nonce_index} (num_nonces {})",
                            sender_id, public_key, gas_key_info.num_nonces,
                        ))
                    },
                )?;
            (current_nonce, DelegateNonceUpdate::GasKey { nonce_index })
        }
    };

    if delegate_nonce.nonce() <= current_nonce {
        result.result = Err(ActionErrorKind::DelegateActionInvalidNonce {
            delegate_nonce: delegate_nonce.nonce(),
            ak_nonce: current_nonce,
        }
        .into());
        return Ok(());
    }

    let upper_bound = apply_state.block_height
        * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER;
    if delegate_nonce.nonce() >= upper_bound {
        result.result = Err(ActionErrorKind::DelegateActionNonceTooLarge {
            delegate_nonce: delegate_nonce.nonce(),
            upper_bound,
        }
        .into());
        return Ok(());
    }
```

**File:** runtime/runtime/src/actions.rs (L745-760)
```rust
    match action {
        Action::DeployContract(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::WithdrawFromGasKey(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
        }
```
