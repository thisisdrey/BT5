### Title
NFT attribute deposits become permanently locked when `PalletFeature::Attributes` is disabled - ([File: substrate/frame/nfts/src/features/attributes.rs])

### Summary
The `pallet-nfts` gates several attribute-related extrinsics behind the `PalletFeature::Attributes` runtime feature flag. Setting an attribute in the `Account(delegate)` namespace reserves a currency deposit from the delegate. That deposit can only be recovered by calling `cancel_approval`/`do_cancel_item_attributes_approval`, but this function *itself* requires `PalletFeature::Attributes` to still be enabled. This mirrors the amAMM bug class: the feature toggle that once allowed a user to lock funds is also required to release them, so if the runtime disables that feature after use, the deposit becomes permanently unrecoverable through the normal call path.

### Finding Description
`do_approve_item_attributes` and `do_cancel_item_attributes_approval` both start with an identical guard: [1](#0-0) 

`do_cancel_item_attributes_approval` is the *only* extrinsic-reachable path that unreserves the deposits accumulated for attributes set by a delegate in the `Account(delegate)` namespace: [2](#0-1) 

Note that these deposits (`Attribute::<T, I>` entries keyed by `AttributeNamespace::Account(delegate)`) are reserved when the delegate calls `set_attribute` while `PalletFeature::Attributes` is enabled (the sibling `do_approve_item_attributes` function shows the same gating pattern, confirming the feature check convention used across the module). Once a collection/item accumulates such deposits, if `T::Features` (checked via `is_pallet_feature_enabled`) is later reconfigured to disable `PalletFeature::Attributes` (e.g., a runtime upgrade changing the `Features` config constant for the pallet instance), every call to `cancel_approval` for that delegate will hit the `ensure!(... MethodDisabled)` guard and abort before reaching the `T::Currency::unreserve` calls.

By contrast, `do_clear_attribute` — which also unreserves attribute deposits — has **no** feature-enabled check at all: [3](#0-2) 

but it requires the caller to supply the attribute `key` and go through namespace/permission checks tied to `CollectionOwner`/`ItemOwner` roles; a delegate's `Account(delegate)`-namespace attribute deposits set via a batched approval are specifically meant to be released in bulk via `cancel_approval`, which is the blocked path. This is the exact bug class from the report: a feature-disable makes deposits placed while the feature was active permanently unrecoverable, because the withdrawal/cancellation logic re-checks the same feature flag instead of always allowing recovery of already-locked funds.

### Impact Explanation
This is a permanent user-fund lock: delegate accounts that reserved balance to write approved attributes cannot get their `T::Currency` reserve back once `PalletFeature::Attributes` is turned off for the pallet instance, since the only extrinsic path that calls `unreserve` for these entries (`cancel_approval`) is itself gated by the same flag. This matches the "permanent user-fund lock" impact category explicitly called out as in-scope.

### Likelihood Explanation
Likelihood is Low-to-Medium: it requires a runtime-level change to `T::Features` (a `Get<PalletFeatures>` config item) disabling `PalletFeature::Attributes` for a live NFTs pallet instance that has holders with pending delegate-attribute deposits. This is not attacker-controlled directly, but it is a legitimate operational action (parachain teams commonly restrict NFT pallet feature sets via runtime upgrades to reduce attack surface or align with governance decisions), and once it happens the fund lock is unconditional and unfixable without a further pallet code fix/migration — there is no in-protocol escape hatch.

### Recommendation
Remove the `PalletFeature::Attributes` guard from `do_cancel_item_attributes_approval` (and audit other cancel/refund paths gated the same way, e.g. any other `unreserve`-performing functions behind feature flags) so that once a deposit has been placed, the corresponding cancellation/recovery extrinsic remains callable regardless of whether the feature is currently enabled. Feature flags should gate the creation of new locked state, not the release of funds already locked under a prior configuration.

### Proof of Concept
1. Configure `pallet-nfts` with `PalletFeature::Attributes` enabled.
2. Item owner approves `delegate` via `approve_item_attributes` (`do_approve_item_attributes`), and `delegate` sets one or more attributes in the `Account(delegate)` namespace via `set_attribute`, reserving `T::Currency` deposits from `delegate`.
3. Runtime is upgraded (or `T::Features` constant changed) so `PalletFeature::Attributes` is no longer enabled.
4. `delegate` calls `cancel_approval` to reclaim the reserved deposit; the call hits `ensure!(Self::is_pallet_feature_enabled(PalletFeature::Attributes), Error::<T, I>::MethodDisabled)` in `do_cancel_item_attributes_approval` at [4](#0-3)  and fails.
5. `delegate`'s reserved balance remains locked indefinitely with no other extrinsic capable of unreserving it, since `set_attribute`/`cancel_approval` are the paired create/destroy operations for these specific deposits.

### Citations

**File:** substrate/frame/nfts/src/features/attributes.rs (L285-337)
```rust
	pub(crate) fn do_clear_attribute(
		maybe_check_origin: Option<T::AccountId>,
		collection: T::CollectionId,
		maybe_item: Option<T::ItemId>,
		namespace: AttributeNamespace<T::AccountId>,
		key: BoundedVec<u8, T::KeyLimit>,
	) -> DispatchResult {
		let (_, deposit) = Attribute::<T, I>::take((collection, maybe_item, &namespace, &key))
			.ok_or(Error::<T, I>::AttributeNotFound)?;

		if let Some(check_origin) = &maybe_check_origin {
			// validate the provided namespace when it's not a root call and the caller is not
			// the same as the `deposit.account` (e.g. the deposit was paid by different account)
			if deposit.account != maybe_check_origin {
				ensure!(
					Self::is_valid_namespace(&check_origin, &namespace, &collection, &maybe_item)?,
					Error::<T, I>::NoPermission
				);
			}

			// can't clear `CollectionOwner` type attributes if the collection/item is locked
			match namespace {
				AttributeNamespace::CollectionOwner => match maybe_item {
					None => {
						let collection_config = Self::get_collection_config(&collection)?;
						ensure!(
							collection_config
								.is_setting_enabled(CollectionSetting::UnlockedAttributes),
							Error::<T, I>::LockedCollectionAttributes
						)
					},
					Some(item) => {
						// NOTE: if the item was previously burned, the ItemConfigOf record
						// might not exist. In that case, we allow to clear the attribute.
						let maybe_is_locked = Self::get_item_config(&collection, &item)
							.map_or(None, |c| {
								Some(c.has_disabled_setting(ItemSetting::UnlockedAttributes))
							});
						if let Some(is_locked) = maybe_is_locked {
							ensure!(!is_locked, Error::<T, I>::LockedItemAttributes);
							// Only the collection's admin can clear attributes in that namespace.
							// e.g. in off-chain mints, the attribute's depositor will be the item's
							// owner, that's why we need to do this extra check.
							ensure!(
								Self::has_role(&collection, &check_origin, CollectionRole::Admin),
								Error::<T, I>::NoPermission
							);
						}
					},
				},
				_ => (),
			};
		}
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L412-423)
```rust
	pub(crate) fn do_cancel_item_attributes_approval(
		check_origin: T::AccountId,
		collection: T::CollectionId,
		item: T::ItemId,
		delegate: T::AccountId,
		witness: CancelAttributesApprovalWitness,
	) -> DispatchResult {
		ensure!(
			Self::is_pallet_feature_enabled(PalletFeature::Attributes),
			Error::<T, I>::MethodDisabled
		);

```

**File:** substrate/frame/nfts/src/features/attributes.rs (L427-453)
```rust
		ItemAttributesApprovalsOf::<T, I>::try_mutate(collection, item, |approvals| {
			approvals.remove(&delegate);

			let mut attributes: u32 = 0;
			let mut deposited: DepositBalanceOf<T, I> = Zero::zero();
			for (_, (_, deposit)) in Attribute::<T, I>::drain_prefix((
				&collection,
				Some(item),
				AttributeNamespace::Account(delegate.clone()),
			)) {
				attributes.saturating_inc();
				deposited = deposited.saturating_add(deposit.amount);
			}
			ensure!(attributes <= witness.account_attributes, Error::<T, I>::BadWitness);

			if !deposited.is_zero() {
				T::Currency::unreserve(&delegate, deposited);
			}

			Self::deposit_event(Event::ItemAttributesApprovalRemoved {
				collection,
				item,
				delegate,
			});
			Ok(())
		})
	}
```
