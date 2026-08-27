### Title
Griefing DoS by front-running a relayed `SignedDelegateAction` (meta transaction) to burn its nonce - (File: `runtime/runtime/src/actions.rs`)

### Summary
The external report describes griefing via front-running EIP-2612 `permit()`: a self-contained, off-chain-signed authorization (signature + nonce) can be extracted from the mempool and replayed by anyone directly to the token, consuming the signer's nonce and causing the original protocol transaction (`supplyWithPermit`/`repayWithPermit`) to revert for no attacker profit. NEAR's meta-transaction (NEP-366) primitive, `SignedDelegateAction`, has the same structural property: it is a self-contained, user-signed authorization (signature over `DelegateAction`, including a `nonce`) that is meant to be wrapped and submitted by a specific relayer, but nothing in validation binds it to that relayer.

### Finding Description
A `SignedDelegateAction` is produced off-chain by a user ("Alice") and handed to a relayer, who wraps it in `Action::Delegate`/`Action::DelegateV2` inside their own transaction and submits it, paying gas [1](#0-0) . On the receiver shard, `apply_delegate_action` processes the embedded `SignedDelegateAction`: it verifies the signature, checks expiry, checks that `delegate_action.sender_id` matches the receipt receiver, and then calls `validate_delegate_action_key`, which validates and **advances** the access key's (or gas key's) nonce before generating the inner receipt [2](#0-1) .

Critically, nothing in this validation path checks *who* wrapped/submitted the `SignedDelegateAction` — the outer transaction's signer (the relayer) is never checked against any specific authorized relayer. Because `DelegateAction`/`SignedDelegateAction` are fully self-contained (all fields needed to reconstruct and resubmit them, including `signature`, are visible once the transaction is observable, e.g. in the mempool or gossiped before inclusion), any third party can copy the exact same `SignedDelegateAction` bytes, wrap them in a new transaction that they sign and pay gas for themselves, and race the legitimate relayer's transaction into a block.

Whichever transaction is processed first succeeds and advances the sender's access-key nonce (`access_key.nonce = delegate_nonce.nonce()`) or gas-key nonce [3](#0-2) . The second submission — the original relayer's transaction, which the user and relayer intended to be the one recorded on-chain — is rejected with `DelegateActionInvalidNonce` since `delegate_nonce.nonce() <= current_nonce` now holds [4](#0-3) . This is exactly analogous to permit-front-running: the attacker gains nothing, but the legitimate relayer's transaction is forced to fail after the relayer has already committed to paying gas for constructing/broadcasting it, and the user's intended action does not execute, requiring them to sign and resend a new meta transaction with a fresh nonce.

This is confirmed further by the test suite, which explicitly demonstrates that "replaying the same delegate (same gas key nonce) is rejected" once the nonce has been consumed by any submission [5](#0-4) , and the NEP-366 design doc itself acknowledges the associated trust assumptions between relayer and user but does not describe any binding of a `SignedDelegateAction` to a specific relayer or outer-transaction signer [6](#0-5) .

### Impact Explanation
This is a griefing/DoS vector, not a fund-theft vector: an attacker with no economic stake in the sender or relayer can consume the meta-transaction's nonce by resubmitting it themselves (paying only their own gas, which they can minimize since the inner actions will simply fail validation for them or succeed harmlessly if they let it through — either way the nonce is consumed). The result is that the relayer's carefully constructed and gas-prepaid transaction is forced to fail on-chain, wasting relayer gas expenditure and delaying/blocking the user's intended action (a `FunctionCall`, `Transfer`, `AddKey`, etc.) until a new `SignedDelegateAction` with a fresh nonce is created and resubmitted. For relayer services processing many meta transactions, this could be used to repeatedly disrupt specific users or relayers with no cost-benefit motive for the attacker beyond griefing (denial-of-service), matching the "Griefing" impact category of the source report.

### Likelihood Explanation
Likelihood is moderate: it requires visibility of a pending `SignedDelegateAction`-carrying transaction before it is included in a block (e.g., via mempool observation, gossip, or an API that surfaces pending transactions), and the ability to submit a competing transaction that gets included first. Because `DelegateAction.nonce` must be strictly greater than the current stored nonce for the access key/gas key (`delegate_nonce.nonce() <= current_nonce` fails validation) [4](#0-3) , submitting the identical extracted `SignedDelegateAction` again from any outer transaction is guaranteed to compete for and consume that exact nonce slot, making the front-run deterministic once observed in time.

### Recommendation
Bind a `SignedDelegateAction` to the specific outer transaction/relayer it was intended for, e.g., by having the user optionally include and sign over an expected relayer `AccountId` (or the outer transaction's signer) as part of the `DelegateAction` payload that is checked in `apply_delegate_action`/`validate_delegate_action_key`, so that only the authorized relayer's wrapping transaction can consume the nonce. Alternatively, document this as an accepted trust/relayer-selection assumption (similar to how `NEP-366`'s docs already discuss relayer trust issues) and ensure relayer implementations do not broadcast or expose `SignedDelegateAction`s in ways that let third parties observe and race them before inclusion.

### Proof of Concept
1. Alice signs a `DelegateAction` (e.g., wrapping an `ft_transfer`-like `FunctionCall` or a `Transfer`) with nonce `N`, and hands the resulting `SignedDelegateAction` to Relayer R off-chain.
2. R wraps it in `Action::Delegate` inside transaction `Tx_R` (signed and paid for by R) and broadcasts `Tx_R`.
3. Attacker M observes the pending `Tx_R` (e.g. via mempool/gossip) and copies the identical `SignedDelegateAction` bytes into their own transaction `Tx_M`, signed and paid for by M, and gets `Tx_M` included first (e.g., with higher priority or race conditions favorable to M).
4. Runtime processes `Tx_M`'s `Action::Delegate` via `apply_delegate_action` → `validate_delegate_action_key`, which succeeds (signature, expiry, sender match, nonce check all pass) and sets `access_key.nonce = N` [7](#0-6) .
5. When `Tx_R` is subsequently processed, `apply_delegate_action` re-validates the same `SignedDelegateAction`; now `delegate_nonce.nonce() (== N) <= current_nonce (== N)`, so it fails with `ActionErrorKind::DelegateActionInvalidNonce`, exactly as exercised by the existing regression test for replayed delegate actions [5](#0-4) .
6. R's transaction fails on-chain (gas wasted by R), and Alice's intended action never executes; Alice must sign and resend a new `DelegateAction` with nonce `N+1`.

### Citations

**File:** docs/architecture/how/meta-tx.md (L40-52)
```markdown
With meta transactions, Alice can create a `DelegateAction`, which is very
similar to a transaction. It also contains a list of actions to execute and a
single receiver for those actions. She signs the `DelegateAction` and forwards
it (off-chain) to a relayer. The relayer wraps it in a transaction, of which the
relayer is the signer and therefore pays the gas costs. If the inner actions
have an attached token balance, this is also paid for by the relayer.

On chain, the `SignedDelegateAction` inside the transaction is converted to an
action receipt with the same `SignedDelegateAction` on the relayer's shard. The
receipt is forwarded to the account from `Alice`, which will unpacked the
`SignedDelegateAction` and verify that it is signed by Alice with a valid Nonce
etc. If all checks are successful, a new action receipt with the inner actions
as body is sent to `FT`. There, the `ft_transfer` call finally executes.
```

**File:** docs/architecture/how/meta-tx.md (L54-80)
```markdown
## Relayer

Meta transactions only work with a relayer. This is an application layer
concept, implemented off-chain. Think of it as a server that accepts a
`SignedDelegateAction`, does some checks on them and eventually forwards it
inside a transaction to the blockchain network.

A relayer may choose to offer their service for free but that's not going to be
financially viable long-term. But they could easily have the user pay using
other means, outside of Near blockchain. And with some tricks, it can even be
paid using fungible tokens on Near.

In the example visualized above, the payment is done using \$FT. Together with
the transfer to John, Alice also adds an action to pay 0.1 \$FT to the relayer.
The relayer checks the content of the `SignedDelegateAction` and only processes
it if this payment is included as the first action. In this way, the relayer
will be paid in the same transaction as John.

Note that the payment to the relayer is still not guaranteed. It could be that
Alice does not have sufficient $FT and the transfer fails. To mitigate, the
relayer should check the $FT balance of Alice first.

Unfortunately, this still does not guarantee that the balance will be high
enough once the meta transaction executes. The relayer could waste NEAR gas
without compensation if Alice somehow reduces her \$FT balance in just the right
moment. Some level of trust between the relayer and its user is therefore
required.
```

**File:** runtime/runtime/src/actions.rs (L437-481)
```rust
pub(crate) fn apply_delegate_action(
    state_update: &mut TrieUpdate,
    apply_state: &ApplyState,
    action_receipt: &VersionedActionReceipt,
    sender_id: &AccountId,
    signed_delegate_action: VersionedSignedDelegateActionRef<'_>,
    result: &mut ActionResult,
) -> Result<(), RuntimeError> {
    // The inner delegate signature is verified below, here on the receiver shard.
    // Meter its verification compute against this shard's `compute_limit`; the gas
    // for it was already burnt at tx conversion on the signer shard. Without the
    // fix the compute is instead mis-charged on the signer shard (which never runs
    // this verify), letting the work escape the receiver shard's budget. See
    // `signature_verification_cost`.
    if apply_state.config.wasm_config.fix_ml_dsa_cost_charging {
        let verify_compute = delegate_signature_verification_compute(
            &apply_state.config.fees,
            signed_delegate_action.delegate_action().public_key(),
        );
        result.compute_usage = safe_add_compute(result.compute_usage, verify_compute)?;
    }
    if !signed_delegate_action.verify() {
        result.result = Err(ActionErrorKind::DelegateActionInvalidSignature.into());
        return Ok(());
    }
    let delegate_action = signed_delegate_action.delegate_action();
    if apply_state.block_height > delegate_action.max_block_height() {
        result.result = Err(ActionErrorKind::DelegateActionExpired.into());
        return Ok(());
    }
    if delegate_action.sender_id().as_str() != sender_id.as_str() {
        result.result = Err(ActionErrorKind::DelegateActionSenderDoesNotMatchTxReceiver {
            sender_id: delegate_action.sender_id().clone(),
            receiver_id: sender_id.clone(),
        }
        .into());
        return Ok(());
    }

    validate_delegate_action_key(state_update, apply_state, delegate_action, result)?;
    if result.result.is_err() {
        // Validation failed. Need to return Ok() because this is not a runtime error.
        // "result.result" will be return to the User as the action execution result.
        return Ok(());
    }
```

**File:** runtime/runtime/src/actions.rs (L632-727)
```rust
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

    let actions = delegate_action.get_actions();

    // The restriction of "function call" access keys:
    // the transaction must contain the only `FunctionCall` if "function call" access key is used
    if let Some(function_call_permission) = access_key.permission.function_call_permission() {
        if actions.len() != 1 {
            result.result = Err(ActionErrorKind::DelegateActionAccessKeyError(
                InvalidAccessKeyError::RequiresFullAccess,
            )
            .into());
            return Ok(());
        }
        if let Some(Action::FunctionCall(function_call)) = actions.get(0) {
            if function_call.deposit > Balance::ZERO {
                result.result = Err(ActionErrorKind::DelegateActionAccessKeyError(
                    InvalidAccessKeyError::DepositWithFunctionCall,
                )
                .into());
                // Before this fix, the missing early return allowed execution
                // to fall through to the receiver_id and method_name checks,
                // which could overwrite this error with a different one.
                if ProtocolFeature::FixDelegateActionDepositWithFunctionCallError
                    .enabled(apply_state.current_protocol_version)
                {
                    return Ok(());
                }
            }
            if delegate_action.receiver_id() != &function_call_permission.receiver_id {
                result.result = Err(ActionErrorKind::DelegateActionAccessKeyError(
                    InvalidAccessKeyError::ReceiverMismatch {
                        tx_receiver: delegate_action.receiver_id().clone(),
                        ak_receiver: function_call_permission.receiver_id.clone(),
                    },
                )
                .into());
                return Ok(());
            }
            if !function_call_permission.method_names.is_empty()
                && function_call_permission
                    .method_names
                    .iter()
                    .all(|method_name| &function_call.method_name != method_name)
            {
                result.result = Err(ActionErrorKind::DelegateActionAccessKeyError(
                    InvalidAccessKeyError::MethodNameMismatch {
                        method_name: function_call.method_name.clone(),
                    },
                )
                .into());
                return Ok(());
            }
        } else {
            // There should Action::FunctionCall when "function call" permission is used
            result.result = Err(ActionErrorKind::DelegateActionAccessKeyError(
                InvalidAccessKeyError::RequiresFullAccess,
            )
            .into());
            return Ok(());
        }
    };

    match nonce_update {
        DelegateNonceUpdate::AccessKey => {
            access_key.nonce = delegate_nonce.nonce();
            set_access_key(state_update, sender_id.clone(), public_key.clone(), &access_key);
        }
        DelegateNonceUpdate::GasKey { nonce_index } => {
            set_gas_key_nonce(
                state_update,
                sender_id.clone(),
                public_key.clone(),
                nonce_index,
                delegate_nonce.nonce(),
            );
        }
    }
```

**File:** test-loop-tests/src/tests/gas_keys.rs (L274-284)
```rust
    // Replaying the same delegate (same gas key nonce) is rejected.
    let block_hash = get_shared_block_hash(&env.node_datas, &env.test_loop.data);
    let replay_tx = SignedTransaction::from_actions(
        next_relayer_nonce(),
        relayer.clone(),
        sender.clone(),
        &relayer_signer,
        vec![Action::DelegateV2(Box::new(signed_delegate))],
        block_hash,
    );
    let replay_outcome = env.rpc_runner().execute_tx(replay_tx, Duration::seconds(5)).unwrap();
```
