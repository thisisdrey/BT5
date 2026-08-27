### Title
Meta-transaction `DelegateAction` signatures lack chain-binding data, enabling cross-chain replay - ([File: core/primitives/src/action/delegate.rs])

### Summary
NEP-366 meta-transactions let an ordinary account holder ("sender") off-chain sign a `DelegateAction` that a relayer later wraps in an on-chain `SignedTransaction`. The signed payload for `DelegateAction`/`DelegateActionV2` only commits to `sender_id`, `receiver_id`, `actions`, `nonce`, `max_block_height`, and `public_key` — there is no chain/genesis identifier bound into the signature, analogous to the ERC20Permit report's missing `chainID`.

### Finding Description
`DelegateAction` is signed via `get_nep461_hash`, which wraps the struct in a `SignableMessage` tagged only with a NEP-based `MessageDiscriminant` (366 or 611), then hashes and signs it: [1](#0-0) [2](#0-1) 

The `DelegateAction`/`DelegateActionV2` struct itself contains no chain- or network-binding field: [3](#0-2) 

Unlike a normal `SignedTransaction`, which includes a `block_hash` field pointing at a specific recent block (implicitly binding the signature to one particular chain's history), a `DelegateAction` is only bounded by `max_block_height` — a plain numeric ceiling, not a hash tied to any specific chain. Verification at execution time (`SignedDelegateAction::verify` / `VersionedSignedDelegateAction::verify`) checks only the signature over this chain-agnostic payload: [4](#0-3) [5](#0-4) 

On-chain validation in `apply_delegate_action` / `validate_delegate_action_key` enforces signature validity, `max_block_height` expiry, sender/receiver matching, and a per-access-key/gas-key nonce strictly greater than the currently stored nonce — all of which are chain-local state checks, not chain-identity checks: [6](#0-5) [7](#0-6) 

If the same `sender_id`/`public_key`/access-key nonce state exists on two distinct NEAR-protocol networks that share ancestral state (e.g., a state-cloned test/canary network, a permanent chain split arising from the shared genesis/state snapshot, or any deployment that forks off an existing chain's state), a previously-collected `DelegateAction` signed by the sender for use on one network can be resubmitted verbatim by anyone holding it (most naturally the relayer, but any party who observed it) on the other network, because nothing in the signed bytes distinguishes the two chains. As long as the access key's nonce has not yet advanced past the delegate's nonce on the second chain and `max_block_height` hasn't been exceeded there, the replay succeeds and the delegated actions (e.g., token transfers, key additions) execute a second time on the sender's behalf without new authorization.

### Impact Explanation
Successful replay lets a third party re-execute a sender's previously authorized `DelegateAction` on a second network sharing state history, causing unauthorized double-execution of delegated actions (e.g., duplicate token transfers/allowances, or replayed `AddKey`/`FunctionCall` actions), i.e., a double-spend/replay of the sender's own authorization across chains — directly resulting in fund loss or authorization escalation for the sender's account.

### Likelihood Explanation
Exploitability requires a second chain/network that shares the sender's account/access-key/nonce state with the origin chain (e.g., a state-forked or state-cloned network) — this is not guaranteed in every deployment, so likelihood is scenario-dependent, but the root cause (absence of chain-binding data in the NEP-366/NEP-611 signing scheme, `SignableMessage`/`DelegateAction`) is present unconditionally in the signing/verification code path reachable by any ordinary account holder using meta-transactions.

### Recommendation
Short term, bind the signed `DelegateAction`/`DelegateActionV2` payload (or the `SignableMessage` wrapper) to a chain-specific identifier such as the genesis hash/chain ID, similar to how ordinary `SignedTransaction`s are implicitly bound via `block_hash`, so a signature produced for one network cannot verify on another network sharing the same account/nonce state. Long term, document the domain-separation guarantees of the NEP-366/NEP-461/NEP-611 signing schemes explicitly, including their behavior under network forks/state clones, similar to the "long term" recommendation in the source report.

### Proof of Concept
Conceptual PoC (cannot be executed without two live NEAR networks sharing state):
1. Deploy/derive Network B from Network A's state at some height (state fork/clone), such that Alice's account, access key, and stored nonce are identical on both networks.
2. Alice signs a `DelegateAction` (per `core/primitives/src/action/delegate.rs`) authorizing a token transfer, with `max_block_height` set generously into the future, intended for submission via a relayer on Network A only.
3. The relayer (or any party who intercepted the `SignedDelegateAction`) submits the same bytes as an `Action::Delegate`/`Action::DelegateV2` inside a transaction on Network B.
4. `apply_delegate_action` on Network B verifies the signature successfully (same hash bytes, same public key), the nonce check passes because Network B's access key nonce hasn't advanced, and the action executes — replaying Alice's authorization on a chain she never intended to authorize it for, as shown by the existing test harness pattern in `runtime/runtime/src/actions.rs` (`test_delegate_action_signature_verification`, `test_validate_delegate_action_key_update_nonce`), which demonstrates that verification and nonce state are the only gates and neither is chain-scoped. [8](#0-7)

### Citations

**File:** core/primitives/src/action/delegate.rs (L46-64)
```rust
pub struct DelegateAction {
    /// Signer of the delegated actions
    pub sender_id: AccountId,
    /// Receiver of the delegated actions.
    pub receiver_id: AccountId,
    /// List of actions to be executed.
    ///
    /// With the meta transactions MVP defined in NEP-366, nested
    /// DelegateActions are not allowed. A separate type is used to enforce it.
    pub actions: Vec<NonDelegateAction>,
    /// Nonce to ensure that the same delegate action is not sent twice by a
    /// relayer and should match for given account's `public_key`.
    /// After this action is processed it will increment.
    pub nonce: Nonce,
    /// The maximal height of the block in the blockchain below which the given DelegateAction is valid.
    pub max_block_height: BlockHeight,
    /// Public key used to sign this delegated action.
    pub public_key: PublicKey,
}
```

**File:** core/primitives/src/action/delegate.rs (L83-96)
```rust
impl SignedDelegateAction {
    pub fn verify(&self) -> bool {
        let delegate_action = &self.delegate_action;
        let hash = delegate_action.get_nep461_hash();
        let public_key = &delegate_action.public_key;

        self.signature.verify(hash.as_ref(), public_key)
    }

    pub fn sign(singer: &Signer, delegate_action: DelegateAction) -> Self {
        let signature = singer.sign(delegate_action.get_nep461_hash().as_bytes());
        Self { delegate_action, signature }
    }
}
```

**File:** core/primitives/src/action/delegate.rs (L210-220)
```rust
impl VersionedSignedDelegateAction {
    pub fn verify(&self) -> bool {
        let hash = self.delegate_action.get_nep461_hash();
        self.signature.verify(hash.as_ref(), self.delegate_action.public_key())
    }

    pub fn sign(signer: &Signer, delegate_action: VersionedDelegateActionPayload) -> Self {
        let signature = signer.sign(delegate_action.get_nep461_hash().as_bytes());
        Self { delegate_action, signature }
    }
}
```

**File:** core/primitives/src/action/delegate.rs (L349-357)
```rust
    /// Delegate action hash used for NEP-461 signature scheme which tags
    /// different messages before hashing
    ///
    /// For more details, see: [NEP-461](https://github.com/near/NEPs/pull/461)
    pub fn get_nep461_hash(&self) -> CryptoHash {
        let signable = SignableMessage::new(&self, SignableMessageType::DelegateAction);
        let bytes = borsh::to_vec(&signable).expect("Failed to deserialize");
        hash(&bytes)
    }
```

**File:** core/primitives/src/signable_message.rs (L97-108)
```rust
impl<'a, T: BorshSerialize> SignableMessage<'a, T> {
    pub fn new(msg: &'a T, ty: SignableMessageType) -> Self {
        let discriminant = ty.into();
        Self { discriminant, msg }
    }

    pub fn sign(&self, signer: &Signer) -> Signature {
        let bytes = borsh::to_vec(&self).expect("Failed to deserialize");
        let hash = hash(&bytes);
        signer.sign(hash.as_bytes())
    }
}
```

**File:** runtime/runtime/src/actions.rs (L458-474)
```rust
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

**File:** runtime/runtime/src/actions.rs (L1494-1545)
```rust
    #[test]
    fn test_validate_delegate_action_key_update_nonce() {
        let (_, signed_delegate_action) = create_delegate_action_receipt();
        let sender_id = &signed_delegate_action.delegate_action.sender_id;
        let sender_pub_key = &signed_delegate_action.delegate_action.public_key;
        let access_key = AccessKey { nonce: 19000000, permission: AccessKeyPermission::FullAccess };

        let apply_state =
            create_apply_state(signed_delegate_action.delegate_action.max_block_height);
        let mut state_update = setup_account(sender_id, sender_pub_key, &access_key);

        // Everything is ok
        let mut result = ActionResult::default();
        validate_delegate_action_key(
            &mut state_update,
            &apply_state,
            (&signed_delegate_action.delegate_action).into(),
            &mut result,
        )
        .expect("Expect ok");
        assert!(result.result.is_ok(), "Result error: {:?}", result.result);

        // Must fail, Nonce had been updated by previous step.
        result = ActionResult::default();
        validate_delegate_action_key(
            &mut state_update,
            &apply_state,
            (&signed_delegate_action.delegate_action).into(),
            &mut result,
        )
        .expect("Expect ok");
        assert_eq!(
            result.result,
            Err(ActionErrorKind::DelegateActionInvalidNonce {
                delegate_nonce: signed_delegate_action.delegate_action.nonce,
                ak_nonce: signed_delegate_action.delegate_action.nonce,
            }
            .into())
        );

        // Increment nonce. Must pass.
        result = ActionResult::default();
        let mut delegate_action = signed_delegate_action.delegate_action.clone();
        delegate_action.nonce += 1;
        validate_delegate_action_key(
            &mut state_update,
            &apply_state,
            (&delegate_action).into(),
            &mut result,
        )
        .expect("Expect ok");
        assert!(result.result.is_ok(), "Result error: {:?}", result.result);
```
