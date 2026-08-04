## Finding [1](#0-0) 

### Title
Permissionless `create_pool` can permanently block privileged `create_pool_with_fee` for the same asset pair - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` exposes an unprivileged, permissionless `create_pool` extrinsic and a privileged `create_pool_with_fee` extrinsic gated by `T::AdminOrigin`. Both ultimately call the shared `do_create_pool` helper, which derives a deterministic `pool_id` from the two asset kinds and inserts it into `Pools` only if the key does not already exist, erroring with `PoolExists` otherwise. Because the `pool_id` is a pure function of the asset pair (no nonce, no randomness), any unprivileged account can pre-emptively call `create_pool` for a known asset pair before governance/`AdminOrigin` gets around to calling `create_pool_with_fee` for that same pair, permanently blocking the privileged call. Since there is no `destroy_pool`/removal path for `Pools` entries in this pallet, the block is irreversible.

### Finding Description
`do_create_pool` computes the deterministic pool key and guards creation with a single existence check: [2](#0-1) 

Both call paths reach this same guarded logic:
- Public/unprivileged: `create_pool(origin, asset1, asset2)` → `ensure_signed` → `do_create_pool(.., None)` [3](#0-2) 
- Privileged: `create_pool_with_fee(origin, creator, asset1, asset2, fee)` → `T::AdminOrigin::ensure_origin` → `do_create_pool(.., Some(fee))` [4](#0-3) 

The `PoolId` is derived purely from `(asset1, asset2)` via `T::PoolLocator::pool_id`, so it is fully predictable off-chain by anyone who knows which asset pair the admin intends to configure with a non-default fee (e.g. a well-known system asset pair on Asset Hub). There is no `destroy_pool`/removal extrinsic in this pallet — a search of the pallet confirms no `Pools::<T>::remove` call site exists — so once `Pools::<T>::contains_key(&pool_id)` is true, it stays true forever.

This exactly mirrors the root cause in the Morpho report: a critical, one-time, key-guarded initialization (`initializeMarket`/`createMarket`) that a privileged actor needs to perform can be permanently pre-empted by anyone calling the equivalent permissionless creation path for the same deterministic key first, and the underlying primitive rejects re-creation once the key exists.

### Impact Explanation
`create_pool_with_fee` is the only way to create a pool that will use a per-pool `LPFee` override from creation (bundling `PoolCreated` + `PoolFeeSet` atomically, with the specified `creator` paying the setup fee/deposit). If an unprivileged user front-runs (or simply preemptively squats) the deterministic pool for that asset pair via ordinary `create_pool`, the `AdminOrigin`-gated call permanently fails with `PoolExists`, and there is no recovery path (no destroy/removal). The intended pool configuration (custom fee, specific `creator`/deposit payer) can never be established for that asset pair — a permanent denial-of-service on a governance/admin-intended market configuration, analogous to the Morpho DoS on `initializeMarket`.

While `set_pool_fee` (also `AdminOrigin`-gated) can retroactively set a fee on an already-existing pool, it cannot fix the `creator`/deposit-payer bookkeeping recorded in `PoolCreated`/deposits taken during `do_create_pool`, and it does not restore the atomic "PoolCreated + PoolFeeSet" intent — the specific privileged flow (`create_pool_with_fee`) is unconditionally and permanently blocked once the id is squatted.

### Likelihood Explanation
No special access (no relayer, validator, collator, leaked key, or governance role) is required — `create_pool` is callable by any signed account and the `pool_id` is deterministically computable from public information (asset pair), so an attacker does not even need to observe a specific pending transaction (unlike classic front-running); they can occupy the slot at any time before the admin acts, for the cost of the `PoolSetupFee`. This makes the griefing cheap and reliable for any asset pair whose future privileged use is publicly anticipated (e.g., system stablecoin pairs).

### Recommendation
Add a privileged path that does not depend on `Pools` not already containing the key, e.g.:
- Allow `AdminOrigin` to call `set_pool_fee` (already possible) combined with reassigning/annotating the recorded `creator`, or
- Provide a `force_create_pool`/`take_over_pool` extrinsic restricted to `AdminOrigin` that can overwrite an existing `PoolInfo` (and refund/adjust the original creator's deposit) when the pool was created by a non-privileged account, or
- Introduce a cooldown/reservation mechanism so `AdminOrigin` can reserve a `pool_id` before public creation is possible for specific asset kinds.

### Proof of Concept
1. Observe (from documentation, prior governance proposals, or common sense) that governance intends to create a pool for asset pair `(Native, WithId(X))` via `create_pool_with_fee` with a custom fee.
2. Any account calls `AssetConversion::create_pool(origin, Box::new(Native), Box::new(WithId(X)))` beforehand — this succeeds and inserts `Pools::<T>::insert(pool_id, PoolInfo { lp_token })` as shown in `do_create_pool`.
3. Governance later calls `AssetConversion::create_pool_with_fee(admin_origin, creator, Box::new(Native), Box::new(WithId(X)), fee)`.
4. This fails with `Error::<T>::PoolExists` because `ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists)` in `do_create_pool` is triggered — confirmed by the existing test `create_same_pool_twice_should_fail`. [5](#0-4) 
5. Because no removal/destroy function exists for `Pools`, the intended admin-configured pool for `(Native, WithId(X))` can never be created.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L440-450)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::create_pool())]
		pub fn create_pool(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_create_pool(&sender, *asset1, *asset2, None)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L618-637)
```rust
		/// Like [`Pallet::create_pool`], but sets an initial per-pool swap `fee` overriding the
		/// global [`Config::LPFee`].
		///
		/// Requires [`Config::AdminOrigin`]. `creator` pays the pool setup fee and deposits.
		/// `fee` must not exceed [`Config::MaxSwapFee`].
		///
		/// Emits both [`Event::PoolCreated`] and [`Event::PoolFeeSet`] on success.
		#[pallet::call_index(6)]
		#[pallet::weight(T::WeightInfo::create_pool_with_fee())]
		pub fn create_pool_with_fee(
			origin: OriginFor<T>,
			creator: T::AccountId,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
			fee: Permill,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin(origin)?;
			Self::do_create_pool(&creator, *asset1, *asset2, Some(fee))?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L740-746)
```rust
			// prepare pool_id
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L246-271)
```rust
#[test]
fn create_same_pool_twice_should_fail() {
	new_test_ext().execute_with(|| {
		let user = 1;
		let token_1 = NativeOrWithId::Native;
		let token_2 = NativeOrWithId::WithId(2);

		create_tokens(user, vec![token_2.clone()]);

		let lp_token = AssetConversion::get_next_pool_asset_id();
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_2.clone()),
			Box::new(token_1.clone())
		));
		let expected_free = lp_token + 1;
		assert_eq!(expected_free, AssetConversion::get_next_pool_asset_id());

		assert_noop!(
			AssetConversion::create_pool(
				RuntimeOrigin::signed(user),
				Box::new(token_2.clone()),
				Box::new(token_1.clone())
			),
			Error::<Test>::PoolExists
		);
```
