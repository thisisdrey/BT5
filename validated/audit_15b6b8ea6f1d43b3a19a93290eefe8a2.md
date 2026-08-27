### Title
`SignedDelegateAction` front-running consumes the sender's nonce, causing legitimate meta-transaction relayer to permanently burn gas - (File: `runtime/runtime/src/actions.rs`)

### Summary
NEAR's meta-transaction (NEP-366) `DelegateAction` mechanism uses the same off-chain-signed-authorization pattern that `TRANSFER_FROM_WITH_PERMIT` uses on EVM chains, and it exhibits the same nonce-frontrunning DOS class: `apply_delegate_action`/`validate_delegate_action_key` only check that the `DelegateAction.nonce` is greater than the sender's on-chain access-key nonce and that `receiver_id == delegate_action.sender_id` — they never bind the delegate action to a specific relayer/outer-transaction signer. [1](#0-0) [2](#0-1) 

### Finding Description
A `SignedDelegateAction` is created and signed off-chain by the sender (`Alice`) and forwarded to a relayer, who wraps it inside an outer `SignedTransaction` (with itself as the outer signer, and `receiver_id` set to the sender) and broadcasts it. Any observer who sees this transaction in flight (e.g. in the mempool, or via the JSON-RPC broadcast, or simply because the wire-format bytes of a `SignedDelegateAction` are visible before inclusion) can copy the exact `SignedDelegateAction` bytes and wrap them into their *own* outer transaction with themselves as signer.

The validation code that decides whether a `DelegateAction` is honored performs three checks: `verify()` on the signature (which only checks *Alice* signed it — not who the outer relayer is), that the current block is below `max_block_height`, and that `sender_id == receiver_id` of the outer transaction: [3](#0-2) 

Then `validate_delegate_action_key` checks and increments the sender's access-key (or gas-key) nonce: [4](#0-3) 

Nothing here ties the `DelegateAction`'s validity to the outer transaction's signer/predecessor being the intended relayer. Consequently, an attacker (or any competing relayer) can front-run the legitimate relayer's transaction, submitting an outer transaction that wraps the identical `SignedDelegateAction`, causing the sender's access-key nonce to be consumed first. When the legitimate relayer's transaction is later processed, `validate_delegate_action_key` will reject it with `DelegateActionInvalidNonce`, exactly mirroring the `TRANSFER_FROM_WITH_PERMIT` frontrunning bug in the report.

Crucially, per the protocol's documented gas-accounting rules, the relayer irrevocably burns the `SEND` cost of the delegate action (and the inner actions it wraps) the moment its outer transaction is converted into a receipt — *before* `validate_delegate_action_key` runs on the sender's shard: [5](#0-4) 

This means that when a front-runner has already consumed the nonce, the honest relayer's competing (now-stale) delegate action fails validation on the sender's shard, but the relayer has already had the `SEND` gas fee for the outer transaction and the wrapped `DelegateAction` burned, and it is not refunded (only remaining/EXEC gas is refunded — `SEND` gas that was already burnt for a nonce that turned out to be invalid is lost). The relayer's tokens are permanently lost due to an attacker who copies public data and races to claim the nonce first.

### Impact Explanation
This is an unprivileged, ordinary-client-reachable DOS + fund-loss vector: any account (no special privileges) can observe a pending meta-transaction and front-run the nonce, forcing the honest relayer to permanently lose the already-burnt `SEND`-phase gas fees for a `DelegateAction` that will now always fail nonce validation. Relayer services (a core use case explicitly documented for NEP-366) that batch or automate many meta-transactions are the primary target and would experience concrete, permanent token loss with every successful front-run, and legitimate user transactions relying on these relayers experience denial of service until they re-sign a fresh `DelegateAction` with a new nonce.

### Likelihood Explanation
Likelihood is moderate: `SignedDelegateAction` payloads are public once broadcast (visible in mempool / JSON-RPC broadcast_tx calls before inclusion), and their nonce/signature/sender fields are trivially copyable into a new outer transaction — no cryptographic secret is required from the attacker, mirroring exactly the reported EIP-2612 permit-frontrunning technique. The main cost to the attacker is the gas required to submit the competing outer transaction, but this is bounded and predictable, making a targeted griefing campaign against a specific relayer economically feasible.

### Recommendation
Consider one or more of:
1. Bind `DelegateAction` validity to a specific relayer, e.g. by including the intended outer-transaction `signer_id` inside the signed `DelegateAction` payload and rejecting execution if the outer transaction's signer does not match.
2. Defer/delay the `SEND`-phase gas burn for the wrapped `DelegateAction` until after nonce validation succeeds on the sender's shard, so that a losing (front-run) relayer is not charged for an action that will unconditionally fail.
3. Document/require that relayer implementations treat `DelegateActionInvalidNonce` failures as a signal to check whether the *intended* effect already landed (analogous to the report's "check current allowance/permit-state" recommendation) before resubmitting, and consider a private/broadcast-protected submission channel for relayers to reduce mempool visibility of unexecuted `SignedDelegateAction`s.

### Proof of Concept
1. Alice signs a `SignedDelegateAction` with `nonce = N` for her access key, intending relayer `R` to submit it.
2. Attacker `A` observes the `SignedDelegateAction` bytes (e.g., from `R`'s broadcast to the network, or from a shared mempool/relayer API) before `R`'s transaction is included.
3. `A` constructs its own `SignedTransaction` with itself as signer/predecessor, `receiver_id = Alice`, and `actions = [Action::Delegate(signed_delegate_action)]` (identical bytes), then submits it with sufficient gas price to be included first.
4. `apply_delegate_action` executes on Alice's shard for `A`'s transaction: signature check passes (Alice signed it, not tied to `A`), `sender_id == receiver_id` passes, nonce `N > current_nonce` passes, and the nonce is bumped to `N` ( [6](#0-5) ).
5. `R`'s transaction, wrapping the same `SignedDelegateAction` with `nonce = N`, is processed afterward. `validate_delegate_action_key` now finds `delegate_nonce.nonce() <= current_nonce` and returns `DelegateActionInvalidNonce` ( [2](#0-1) ), yet `R` has already had the `SEND`-phase gas for this delegate/inner-action bundle burnt as documented in `meta-tx.md` — an irrecoverable loss.

This directly parallels the report's PoC where `attacker=address(1337)` calls `permit()` first, consuming the nonce and causing the legitimate `router.execute()` call (with `TRANSFER_FROM_WITH_PERMIT`) to revert.

### Citations

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

**File:** runtime/runtime/src/actions.rs (L586-639)
```rust
    // A plain nonce advances the single access_key.nonce and forbids gas keys;
    // a gas key nonce advances one of the gas key's nonces selected by
    // nonce_index.
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
```

**File:** runtime/runtime/src/actions.rs (L712-717)
```rust

    match nonce_update {
        DelegateNonceUpdate::AccessKey => {
            access_key.nonce = delegate_nonce.nonce();
            set_access_key(state_update, sender_id.clone(), public_key.clone(), &access_key);
        }
```

**File:** docs/architecture/how/meta-tx.md (L195-214)
```markdown
Ok, now adapt for meta transactions. Let's assume Alice uses a relayer to
execute actions with Bob as the receiver.

1. The relayer purchases the gas for all inner actions, plus the gas for the
   delegate action wrapping them.
2. The cost of sending the inner actions and the delegate action from the
   relayer to Alice's shard will be burned immediately. The condition `relayer
   == Alice` determines which action `SEND` cost is taken (`sir` or `not_sir`).
   Let's call this `SEND(1)`.
3. On Alice's shard, the delegate action is executed, thus the `EXEC` gas cost
   for it is burned. Alice sends the inner actions to Bob's shard. Therefore, we
   burn the `SEND` fee again. This time based on `Alice == Bob` to figure out
   `sir` or `not_sir`. Let's call this `SEND(2)`.
4. On Bob's shard, we execute all inner actions and burn their `EXEC` cost.

Each of these steps should make sense and not be too surprising. But the
consequence is that the implicit costs paid at the relayer's shard are
`SEND(1)` + `SEND(2)` + `EXEC` for all inner actions plus `SEND(1)` + `EXEC` for
the delegate action. This might be surprising but hopefully with this
explanation it makes sense now!
```
