The premise of this question is invalid because nested `Action::Delegate` inside a `DelegateAction.actions` list is structurally impossible in this codebase, not merely discouraged by convention.

**Why nesting cannot occur:**

`DelegateAction.actions` is typed as `Vec<NonDelegateAction>`, not `Vec<Action>` [1](#0-0) . `NonDelegateAction` wraps a private `Action` and can only be constructed through two paths, both of which explicitly reject delegate variants:

1. `TryFrom<Action> for NonDelegateAction` returns `Err(IsDelegateAction)` if `action.is_delegate()` is true [2](#0-1) .
2. Borsh deserialization of `NonDelegateAction` explicitly checks the discriminant byte against `DELEGATE_VARIANT_NUMBERS` (the `Delegate`/`DelegateV2` discriminants) and returns a hard `InvalidInput` parse error before any inner action is even constructed [3](#0-2) .

Since an attacker's `SignedTransaction` must be deserialized via Borsh to reach the runtime at all, any wire-level attempt to smuggle a nested `Action::Delegate`/`Action::DelegateV2` inside a `DelegateAction.actions` list fails at deserialization time, before `validate_delegate_action` or `validate_actions_with_mode` ever run [4](#0-3) . This is also covered by an existing regression test, `test_delegate_action_deserialization`, which asserts that a Borsh-serialized nested delegate action fails with `ErrorKind::InvalidInput` [5](#0-4) .

**Consequence for the described attack:** Because `delegate_action.actions` can never contain a `Delegate`/`DelegateV2` variant, `validate_actions_with_mode` is only ever invoked recursively exactly one level deep (outer transaction actions → inner delegate actions), and there is no further recursion level where a `DeleteAccount` could hide non-terminally under a second nested `Delegate`. The `DeleteActionMustBeFinal` check at [6](#0-5)  is applied to both the outer action list and, separately, to the inner delegate's action list [7](#0-6) , and since no third level of nesting is reachable, there is no bypass window as hypothesized.

### No vulnerability found for this question.

### Citations

**File:** core/primitives/src/action/delegate.rs (L51-55)
```rust
    /// List of actions to be executed.
    ///
    /// With the meta transactions MVP defined in NEP-366, nested
    /// DelegateActions are not allowed. A separate type is used to enforce it.
    pub actions: Vec<NonDelegateAction>,
```

**File:** core/primitives/src/action/delegate.rs (L425-431)
```rust
    impl TryFrom<Action> for NonDelegateAction {
        type Error = IsDelegateAction;

        fn try_from(action: Action) -> Result<Self, IsDelegateAction> {
            if action.is_delegate() { Err(IsDelegateAction) } else { Ok(Self(action)) }
        }
    }
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

**File:** core/primitives/src/action/delegate.rs (L581-600)
```rust
    #[test]
    fn test_delegate_action_deserialization() {
        // Expected an error. Buffer is empty
        assert_eq!(
            NonDelegateAction::try_from_slice(Vec::new().as_ref()).map_err(|e| e.kind()),
            Err(ErrorKind::InvalidData)
        );

        let delegate_action = create_delegate_action(Vec::<Action>::new());
        let serialized_non_delegate_action = borsh::to_vec(&delegate_action).expect("Expect ok");

        // Expected Action::Delegate has not been moved in enum Action
        assert_eq!(serialized_non_delegate_action[0], ACTION_DELEGATE_NUMBER);

        // Expected a nested DelegateAction error
        assert_eq!(
            NonDelegateAction::try_from_slice(&serialized_non_delegate_action)
                .map_err(|e| e.kind()),
            Err(ErrorKind::InvalidInput)
        );
```

**File:** runtime/runtime/src/action_validation.rs (L99-103)
```rust
    while let Some(action) = iter.next() {
        if let Action::DeleteAccount(_) = action {
            if iter.peek().is_some() {
                return Err(ActionsValidationError::DeleteActionMustBeFinal);
            }
```

**File:** runtime/runtime/src/action_validation.rs (L182-219)
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
    let actions = delegate_action.get_actions();
    let inner_receiver =
        if ProtocolFeature::FixDelegatedDeterministicStateInit.enabled(current_protocol_version) {
            // This is the correct receiver id to use for the check.
            delegate_action.receiver_id()
        } else {
            // This is a bug fixed with `FixDelegatedDeterministicStateInit` that
            // validated against the wrong id. This makes it impossible to
            // initialize deterministic accounts from meta transactions.
            // The bug cannot be abused, if someone crafts a state init that passes
            // validation here, it will fail when it is checked as incoming receipt.
            receiver
        };
    validate_actions_with_mode(
        limit_config,
        &actions,
        inner_receiver,
        current_protocol_version,
        mode,
    )?;
```
