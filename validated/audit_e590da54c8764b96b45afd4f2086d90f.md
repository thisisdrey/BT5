### Title
Underpriced `destroy_accounts` loop iterations when accounts are skipped due to holds/freezes - (File: substrate/frame/assets/src/functions.rs)

### Summary
The `destroy_accounts` extrinsic charges post-dispatch weight based on the number of accounts *actually removed*, but the internal loop iterates and performs storage reads for up to `RemoveItemsLimit` accounts regardless of whether they are removed. A "stale cleanup mix" of accounts (some destroyable, some not) produces a valid input shape that the benchmark's happy-path (`add_sufficients`, all-removable) does not model.

### Finding Description
`destroy_accounts` is dispatched with a pre-charged worst-case weight `T::WeightInfo::destroy_accounts(T::RemoveItemsLimit::get())`, and after execution the weight is corrected to the actual cost using `T::WeightInfo::destroy_accounts(removed_accounts)`: [1](#0-0) 

The actual work is done in `do_destroy_accounts`, which iterates `Account::<T,I>::iter_prefix(&id)` up to `max_items` entries via `enumerate()`. Crucially, the loop counter `i` (which governs the break condition) advances on every iterated entry, but `dead_accounts` (which determines the weight-refund count `removed_accounts`) is only pushed to when `ensure_account_can_die` succeeds *and* `dead_account` returns `Remove`: [2](#0-1) 

`ensure_account_can_die` fails for any account currently on hold or frozen for the asset: [3](#0-2) 

When it fails, the loop simply `continue`s — the account is neither modified nor counted, yet the iteration still consumed a "slot" toward `max_items` and performed the `Account` prefix read plus a `T::Holder::balance_on_hold` and `T::Freezer::frozen_balance` lookup. The benchmark for `destroy_accounts(c)` only measures the case where all `c` accounts are cleanly removable (populated via `add_sufficients`), so it captures per-removed-account cost (`reads((4 or 5 or 7).mul(c))`, `writes((4 or 6 or 8).mul(c))`) but has no term for accounts that are iterated-but-skipped: [4](#0-3) [5](#0-4) 

Since `destroy_accounts` can be called by any signed account (not just the asset owner): [6](#0-5) 

if an asset's `Destroying` accounts happen to include many frozen/held accounts (e.g. accounts that were free of holds/freezes when `do_start_destroy`'s guard was checked — `!T::Holder::contains_holds` / `!T::Freezer::contains_freezes` — but subsequently had a freeze or hold placed on them by another pallet using the `MutateFreeze`/`MutateHold` traits), then a `destroy_accounts` call that iterates the full `RemoveItemsLimit` window but removes zero (or few) accounts is refunded down to `T::WeightInfo::destroy_accounts(0)` (or a low `c`), while performing up to `RemoveItemsLimit` extra storage reads that are not reflected in the charged weight at all.

### Impact Explanation
This is a benchmark/implementation drift: a valid, permissionless input shape (a "stuck" mix of accounts under a `Destroying` asset) causes real per-call storage-read cost that is not billed, because the refund is keyed to `dead_accounts.len()` rather than to the number of accounts actually iterated. Repeated calls of this shape across many blocks could allow an attacker to extract more state-read work per unit of charged weight than the benchmarked worst case assumes, which falls into the "public underpriced work that degrades block production" impact category.

### Likelihood Explanation
Exploitability is constrained by several preconditions that I could not fully verify in this review:
- The attacker must control (or be) the owner of an asset that is `Destroying`, and must be able to place a hold or freeze on specific asset accounts of that asset *after* `do_start_destroy`'s no-holds/no-freezes gate has passed. This requires some other pallet in the runtime that exposes a user-triggerable path to `MutateFreeze`/`MutateHold` for `pallet-assets` accounts (e.g. via `AssetsFreezer`/`AssetsHolder`). I was not able to confirm within the indexed code whether asset-hub runtimes wire up such a user-facing trigger for arbitrary self-freezing on arbitrary assets.
- The magnitude of the discrepancy is bounded by `T::RemoveItemsLimit::get()` (observed as `[0, 1000]` in benchmarks), meaning at most ~1000-2000 extra reads per call go unbilled — a real but not catastrophic amount of extra work per extrinsic, and it does not touch writes/PoV in the skipped case (only reads).
- The reviewed weight files (`pallet_assets_local.rs`, `pallet_assets_pool.rs`, `pallet_assets_foreign.rs` for asset-hub-westend/rococo) all charge the read/write multiplier strictly on `c` = removed count, confirming the drift exists in the currently shipped weights.

### Recommendation
Have `do_destroy_accounts` return (and have the caller bill for) the number of accounts actually *iterated* (i.e. `i` at loop exit), not just `dead_accounts.len()`, or add a dedicated benchmark/weight component for skipped (held/frozen) accounts so that the charged weight reflects the true number of `ensure_account_can_die` checks performed, independent of how many of them succeed.

### Proof of Concept
Conceptual PoC (not fully verified against a concrete asset-hub runtime wiring):
1. As an attacker, create an asset via `create`/`force_create` and mint `RemoveItemsLimit` sufficient accounts (`add_sufficients`-equivalent).
2. Call `freeze_asset` then `start_destroy` while none of the accounts are frozen/held, satisfying `do_start_destroy`'s `contains_holds`/`contains_freezes` guard.
3. Before calling `destroy_accounts`, use whatever runtime-exposed pallet implements `fungibles::MutateFreeze`/`MutateHold` for this asset (if present) to place a freeze/hold on every one of the minted accounts.
4. Call `destroy_accounts`. The loop in `do_destroy_accounts` iterates up to `RemoveItemsLimit` accounts, calling `ensure_account_can_die` for each (2 extra storage reads per account) and `continue`-ing without removing any of them.
5. Post-dispatch weight is corrected to `T::WeightInfo::destroy_accounts(0)`, i.e. the flat base cost, even though up to `RemoveItemsLimit` iterations and reads occurred — repeat across many transactions/blocks to accumulate unbilled read work. [7](#0-6)

### Citations

**File:** substrate/frame/assets/src/lib.rs (L968-978)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::destroy_accounts(T::RemoveItemsLimit::get()))]
		pub fn destroy_accounts(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			let id: T::AssetId = id.into();
			let removed_accounts = Self::do_destroy_accounts(id, T::RemoveItemsLimit::get())?;
			Ok(Some(T::WeightInfo::destroy_accounts(removed_accounts)).into())
		}
```

**File:** substrate/frame/assets/src/functions.rs (L99-106)
```rust
	pub(super) fn ensure_account_can_die(id: T::AssetId, who: &T::AccountId) -> DispatchResult {
		ensure!(
			T::Holder::balance_on_hold(id.clone(), who).is_none(),
			Error::<T, I>::ContainsHolds
		);
		ensure!(T::Freezer::frozen_balance(id, who).is_none(), Error::<T, I>::ContainsFreezes);
		Ok(())
	}
```

**File:** substrate/frame/assets/src/functions.rs (L825-860)
```rust
	pub(super) fn do_destroy_accounts(
		id: T::AssetId,
		max_items: u32,
	) -> Result<u32, DispatchError> {
		let mut dead_accounts: Vec<T::AccountId> = vec![];
		let mut remaining_accounts = 0;
		Asset::<T, I>::try_mutate_exists(&id, |maybe_details| -> Result<(), DispatchError> {
			let mut details = maybe_details.as_mut().ok_or(Error::<T, I>::Unknown)?;
			// Should only destroy accounts while the asset is in a destroying state
			ensure!(details.status == AssetStatus::Destroying, Error::<T, I>::IncorrectStatus);

			for (i, (who, mut v)) in Account::<T, I>::iter_prefix(&id).enumerate() {
				if Self::ensure_account_can_die(id.clone(), &who).is_err() {
					continue;
				}
				// unreserve the existence deposit if any
				if let Some((depositor, deposit)) = v.reason.take_deposit_from() {
					T::Currency::unreserve(&depositor, deposit);
				} else if let Some(deposit) = v.reason.take_deposit() {
					T::Currency::unreserve(&who, deposit);
				}
				if let Remove = Self::dead_account(&who, &mut details, &v.reason, false) {
					Account::<T, I>::remove(&id, &who);
					dead_accounts.push(who);
				} else {
					// deposit may have been released, need to update `Account`
					Account::<T, I>::insert(&id, &who, v);
					defensive!("destroy did not result in dead account?!");
				}
				if i + 1 >= (max_items as usize) {
					break;
				}
			}
			remaining_accounts = details.accounts;
			Ok(())
		})?;
```

**File:** substrate/frame/assets/src/benchmarking.rs (L197-213)
```rust
	destroy_accounts {
		let c in 0 .. T::RemoveItemsLimit::get();
		let (asset_id, caller, _) = create_default_asset::<T, I>(true);
		add_sufficients::<T, I>(caller.clone(), c);
		Assets::<T, I>::freeze_asset(
			SystemOrigin::Signed(caller.clone()).into(),
			asset_id.clone(),
		)?;
		Assets::<T,I>::start_destroy(SystemOrigin::Signed(caller.clone()).into(), asset_id.clone())?;
	}:_(SystemOrigin::Signed(caller), asset_id.clone())
	verify {
		assert_last_event::<T, I>(Event::AccountsDestroyed {
			asset_id: asset_id.into(),
			accounts_destroyed: c,
			accounts_remaining: 0,
		}.into());
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/weights/pallet_assets_local.rs (L120-134)
```rust
	fn destroy_accounts(c: u32, ) -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `137495 + c * (653 ±0)`
		//  Estimated: `3675 + c * (2886 ±0)`
		// Minimum execution time: 20_885_000 picoseconds.
		Weight::from_parts(21_369_000, 0)
			.saturating_add(Weight::from_parts(0, 3675))
			// Standard Error: 72_371
			.saturating_add(Weight::from_parts(86_343_961, 0).saturating_mul(c.into()))
			.saturating_add(T::DbWeight::get().reads(2))
			.saturating_add(T::DbWeight::get().reads((7_u64).saturating_mul(c.into())))
			.saturating_add(T::DbWeight::get().writes(1))
			.saturating_add(T::DbWeight::get().writes((8_u64).saturating_mul(c.into())))
			.saturating_add(Weight::from_parts(0, 2886).saturating_mul(c.into()))
	}
```
