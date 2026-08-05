[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** substrate/frame/nfts/src/features/attributes.rs (L59-67)
```rust
		ensure!(
			Self::is_pallet_feature_enabled(PalletFeature::Attributes),
			Error::<T, I>::MethodDisabled
		);

		ensure!(
			Self::is_valid_namespace(&origin, &namespace, &collection, &maybe_item)?,
			Error::<T, I>::NoPermission
		);
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L91-91)
```rust
		let attribute = Attribute::<T, I>::get((collection, maybe_item, &namespace, &key));
```

**File:** substrate/frame/nfts/src/lib.rs (L1405-1413)
```rust
		/// Set an attribute for a collection or item.
		///
		/// Origin must be Signed and must conform to the namespace ruleset:
		/// - `CollectionOwner` namespace could be modified by the `collection` Admin only;
		/// - `ItemOwner` namespace could be modified by the `maybe_item` owner only. `maybe_item`
		///   should be set in that case;
		/// - `Account(AccountId)` namespace could be modified only when the `origin` was given a
		///   permission to do so;
		///
```

**File:** substrate/frame/nfts/src/lib.rs (L1464-1477)
```rust
		#[pallet::call_index(20)]
		#[pallet::weight(T::WeightInfo::force_set_attribute())]
		pub fn force_set_attribute(
			origin: OriginFor<T>,
			set_as: Option<T::AccountId>,
			collection: T::CollectionId,
			maybe_item: Option<T::ItemId>,
			namespace: AttributeNamespace<T::AccountId>,
			key: BoundedVec<u8, T::KeyLimit>,
			value: BoundedVec<u8, T::ValueLimit>,
		) -> DispatchResult {
			T::ForceOrigin::ensure_origin(origin)?;
			Self::do_force_set_attribute(set_as, collection, maybe_item, namespace, key, value)
		}
```

**File:** substrate/frame/nfts/src/impl_nonfungibles.rs (L268-282)
```rust
	fn set_attribute(
		collection: &Self::CollectionId,
		item: &Self::ItemId,
		key: &[u8],
		value: &[u8],
	) -> DispatchResult {
		Self::do_force_set_attribute(
			None,
			*collection,
			Some(*item),
			AttributeNamespace::Pallet,
			Self::construct_attribute_key(key.to_vec())?,
			Self::construct_attribute_value(value.to_vec())?,
		)
	}
```
