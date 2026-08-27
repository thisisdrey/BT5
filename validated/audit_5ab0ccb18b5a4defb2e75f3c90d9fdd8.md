Based on the code, this attack is not feasible.

## Analysis

`NonDelegateAction` is protected against nested delegate actions **at the raw byte level**, not just via a typed `TryFrom` check. Its custom `BorshDeserialize` implementation reads the discriminant byte first and rejects it immediately if it matches a delegate variant, before any further parsing occurs: [1](#0-0) 

This means an attacker cannot bypass the check "by hand-crafting bytes" — the discriminant check happens on the raw wire bytes themselves, independent of whether the typed constructor (`TryFrom<Action>`) is used. This is explicitly tested and cross-checked: [2](#0-1) 

Additionally, `DelegateAction.actions` is a flat `Vec<NonDelegateAction>` (one level deep) — `NonDelegateAction` wraps a single `Action` that is structurally guaranteed not to be `Delegate`/`DelegateV2`: [3](#0-2) [4](#0-3) 

Since nesting is structurally impossible (both at the type level and at the raw borsh byte level), there is no way to build "deeply nested/self-referential structures." Vector serialization/deserialization is a linear loop over elements, not recursive-by-depth, so `get_nep461_hash` (which just borsh-serializes the flat `DelegateAction`/`SignableMessage`) has bounded, shallow stack usage: [5](#0-4) [6](#0-5) 

Furthermore, the action count itself is capped by `max_actions_per_receipt` well before execution reaches `apply_delegate_action`/`verify()`: [7](#0-6) 

`apply_delegate_action` in `runtime/runtime/src/actions.rs` calls `signed_delegate_action.verify()` which computes `get_nep461_hash()` over this already-bounded, non-recursive structure: [8](#0-7) 

#No vulnerability found for this question.

### Citations

**File:** core/primitives/src/action/delegate.rs (L46-55)
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
```

**File:** core/primitives/src/action/delegate.rs (L344-357)
```rust
impl DelegateAction {
    pub fn get_actions(&self) -> Vec<Action> {
        self.actions.iter().map(|a| a.clone().into()).collect()
    }

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

**File:** core/primitives/src/action/delegate.rs (L371-371)
```rust
pub struct NonDelegateAction(Action);
```

**File:** core/primitives/src/action/delegate.rs (L433-443)
```rust
    impl borsh::de::BorshDeserialize for NonDelegateAction {
        fn deserialize_reader<R: Read>(rd: &mut R) -> ::core::result::Result<Self, Error> {
            match u8::deserialize_reader(rd)? {
                n if DELEGATE_VARIANT_NUMBERS.contains(&n) => Err(Error::new(
                    ErrorKind::InvalidInput,
                    "DelegateAction mustn't contain a nested one",
                )),
                n => borsh::de::EnumExt::deserialize_variant(rd, n).map(Self),
            }
        }
    }
```

**File:** core/primitives/src/action/delegate.rs (L509-539)
```rust
    #[test]
    fn test_delegate_variant_encodings_match() {
        let delegate_v2: Action = VersionedSignedDelegateAction {
            delegate_action: DelegateActionV2 {
                sender_id: "alice.near".parse().unwrap(),
                receiver_id: "bob.near".parse().unwrap(),
                actions: vec![],
                nonce: TransactionNonce::from_nonce_and_index(1, 0),
                max_block_height: 1000,
                public_key: PublicKey::empty(KeyType::ED25519),
            }
            .into(),
            signature: Signature::empty(KeyType::ED25519),
        }
        .into();
        let delegates = [create_delegate_action(vec![]), delegate_v2];

        for action in &delegates {
            assert!(action.is_delegate());
            let bytes = borsh::to_vec(action).unwrap();
            assert!(DELEGATE_VARIANT_NUMBERS.contains(&bytes[0]));
            // NonDelegateAction refuses it via both the typed and borsh paths.
            assert!(NonDelegateAction::try_from(action.clone()).is_err());
            assert_eq!(
                NonDelegateAction::try_from_slice(&bytes).map_err(|e| e.kind()),
                Err(ErrorKind::InvalidInput)
            );
            #[cfg(feature = "schemars")]
            assert!(DELEGATE_VARIANT_NAMES.contains(&action.as_ref()));
        }
        assert_eq!(DELEGATE_VARIANT_NUMBERS.len(), delegates.len());
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

**File:** runtime/runtime/src/action_validation.rs (L182-199)
```rust
fn validate_delegate_action(
    limit_config: &LimitConfig,
    delegate_action: VersionedDelegateActionRef<'_>,
    receiver: &AccountId,
    current_protocol_version: ProtocolVersion,
    mode: ValidateReceiptMode,
) -> Result<(), ActionsValidationError> {
    // Check the count before `get_actions()` clones the list, so a huge
    // nested-action list can't force the allocation before being rejected.
    // Consensus-neutral: same `TotalNumberOfActionsExceeded` as the check in
    // `validate_actions_with_mode` below.
    let num_actions = delegate_action.actions().len() as u64;
    if num_actions > limit_config.max_actions_per_receipt {
        return Err(ActionsValidationError::TotalNumberOfActionsExceeded {
            total_number_of_actions: num_actions,
            limit: limit_config.max_actions_per_receipt,
        });
    }
```

**File:** runtime/runtime/src/actions.rs (L437-461)
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
```
