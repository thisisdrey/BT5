## Title
`pallet-psm::set_minting_fee` / `set_redemption_fee` accept fees up to 100% with no upper-bound check, unlike the analogous `pallet-asset-conversion::set_pool_fee` - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
This is a local analog of the BathToken "no cap on fees" DOS pattern: an admin-facing fee-setter accepts an arbitrary `Permill` without validating it against any maximum, whereas the sibling pallet in the same repository (`pallet-asset-conversion::set_pool_fee` / `create_pool_with_fee`) explicitly enforces `ensure!(fee <= T::MaxSwapFee::get(), Error::<T>::FeeTooHigh)` before persisting the fee.

### Finding Description
`Pallet::set_minting_fee` and `Pallet::set_redemption_fee` in `substrate/frame/psm/src/lib.rs` (lines ~1076-1097 and ~1119-1141) write an admin-supplied `Permill` fee directly into `MintingFee`/`RedemptionFee` storage with no comparison against any `MaxFee`-style bound: [1](#0-0) [2](#0-1) 

Contrast this with the equivalent per-pool fee setter in `pallet-asset-conversion`, added in the same repository, which explicitly caps the admin-settable fee: [3](#0-2) 

Because `Permill` is internally bounded to `[0, 1_000_000]` (i.e. `[0%, 100%]`) by the `PerThing` type itself, the PSM fee cannot literally exceed 100% the way the original BathToken bug allowed (>100% causing arithmetic underflow). However, the missing bound still reproduces the core broken invariant from the external report: **there is no governance-independent ceiling preventing the PSM admin from setting the fee to 100%**, which the pallet's own test suite (`fee_100_percent`) confirms is accepted and results in the entire redeemed/minted amount being seized as fee, leaving the user with zero output for their deposited funds: [4](#0-3) 

The `Full`-privilege admin (`ensure_psm_admin(... |l| l.can_set_fees())`) can call `set_minting_fee`/`set_redemption_fee` for any approved external asset and unilaterally set the fee to 100% with a single call, with no cap enforced in the pallet itself (unlike `MaxSwapFee` in `asset-conversion`).

### Impact Explanation
A PSM admin can silently set `RedemptionFee`/`MintingFee` to 100% for any approved external asset pair. Any user who then calls `redeem`/`mint` without checking the current fee, or whose `max_fee` slippage parameter happens to tolerate it, will burn/deposit their full amount and receive nothing back — an entire-amount value loss for that user, occurring atomically within pallet logic rather than through governance misuse alone. This matches the "underpriced/overcharged public work causing fund loss" class from the impact gate, since ordinary signed `mint`/`redeem` calls are public entry points and the fee value that determines the payout is unbounded by the pallet's own invariants.

### Likelihood Explanation
Per the task's "Discard" list, "privileged governance or admin abuse as the root cause" is explicitly excluded. The `full_admin`/`can_set_fees()` origin is a privileged internal role, and setting the fee to 100% is itself an admin action, not something an unprivileged attacker can trigger. This weakens the case to essentially a governance-hygiene/consistency gap (missing the same guard the sibling pallet already has) rather than an unprivileged, publicly triggerable exploit path. Likelihood of this qualifying as a "live-scope" HackenProof-eligible finding is low precisely because the root cause requires admin action, and the type-level bound (`Permill` ⊆ [0%,100%]) already prevents the underflow/DOS mechanics that made the original C4 finding severe.

### Recommendation
Add an explicit fee cap analogous to `Config::MaxSwapFee` in `pallet-asset-conversion`:
- Introduce `Config::MaxMintingFee` / `Config::MaxRedemptionFee` (or a shared `MaxFee`) constants.
- In `set_minting_fee` and `set_redemption_fee`, add `ensure!(fee <= T::MaxFee::get(), Error::<T>::FeeTooHigh)` before writing to storage, mirroring `set_pool_fee`'s pattern in `pallet-asset-conversion`.

### Proof of Concept
Using the existing pallet test harness (`substrate/frame/psm/src/tests.rs`):
1. Admin calls `Psm::set_redemption_fee(RuntimeOrigin::root(), INTERNAL_ASSET_ID, USDC_ASSET_ID, Permill::from_percent(100))` — succeeds with no error (confirmed by the pallet's own `fee_100_percent` test).
2. A user calls `Psm::redeem(..., redeem_amount, Permill::from_percent(100))`.
3. The user's internal asset is burned but they receive `alice_usdc_before` unchanged (zero USDC out), while the full `redeem_amount` is credited to `INSURANCE_FUND` as fee — full value loss for the redeemer, with no pallet-level ceiling having prevented the 100% fee from being configured in the first place. [4](#0-3) 

**Caveat**: Because the root cause requires an admin/privileged call (`Full`-privilege `set_*_fee`), and the impact gate explicitly excludes "privileged governance or admin abuse as the root cause," this finding is a **hygiene/defense-in-depth gap** relative to the sibling `asset-conversion` pallet rather than a fully qualifying unprivileged-attacker analog of the original DOS report. I present it as the closest local structural analog found, but flag that it may not clear the strict impact-gate bar on likelihood grounds.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L1077-1097)
```rust
		pub fn set_minting_fee(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			fee: Permill,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_set_fees())?;
			ensure!(
				ExternalAssets::<T>::contains_key(&internal_asset, &external_asset),
				Error::<T>::AssetNotApproved
			);
			let old_value = MintingFee::<T>::get(&internal_asset, &external_asset);
			MintingFee::<T>::insert(&internal_asset, &external_asset, fee);
			Self::deposit_event(Event::MintingFeeUpdated {
				internal_asset,
				external_asset,
				old_value,
				new_value: fee,
			});
			Ok(())
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1121-1141)
```rust
		pub fn set_redemption_fee(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			fee: Permill,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_set_fees())?;
			ensure!(
				ExternalAssets::<T>::contains_key(&internal_asset, &external_asset),
				Error::<T>::AssetNotApproved
			);
			let old_value = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			RedemptionFee::<T>::insert(&internal_asset, &external_asset, fee);
			Self::deposit_event(Event::RedemptionFeeUpdated {
				internal_asset,
				external_asset,
				old_value,
				new_value: fee,
			});
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L639-658)
```rust
		/// Set the per-pool swap `fee` for an existing pool, overriding the global
		/// [`Config::LPFee`].
		///
		/// Requires [`Config::AdminOrigin`]. `fee` must not exceed [`Config::MaxSwapFee`].
		///
		/// Emits [`Event::PoolFeeSet`] on success.
		#[pallet::call_index(7)]
		#[pallet::weight(T::WeightInfo::set_pool_fee())]
		pub fn set_pool_fee(
			origin: OriginFor<T>,
			pool_id: T::PoolId,
			fee: Permill,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin(origin)?;
			ensure!(fee <= T::MaxSwapFee::get(), Error::<T>::FeeTooHigh);
			ensure!(Pools::<T>::contains_key(&pool_id), Error::<T>::PoolNotFound);
			PoolFees::<T>::insert(&pool_id, fee);
			Self::deposit_event(Event::PoolFeeSet { pool_id, fee });
			Ok(())
		}
```

**File:** substrate/frame/psm/src/tests.rs (L484-507)
```rust
	#[test]
	fn fee_100_percent() {
		ExtBuilder::default().mints(ALICE, 5000 * INTERNAL_UNIT).build_and_execute(|| {
			set_redemption_fee(USDC_ASSET_ID, Permill::from_percent(100));

			let redeem_amount = 1000 * INTERNAL_UNIT;
			let alice_usdc_before = get_asset_balance(USDC_ASSET_ID, ALICE);
			let insurance_internal_before = get_asset_balance(INTERNAL_ASSET_ID, INSURANCE_FUND);

			assert_ok!(Psm::redeem(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				redeem_amount,
				Permill::from_percent(100)
			));

			assert_eq!(get_asset_balance(USDC_ASSET_ID, ALICE), alice_usdc_before);
			assert_eq!(
				get_asset_balance(INTERNAL_ASSET_ID, INSURANCE_FUND),
				insurance_internal_before + redeem_amount
			);
		});
	}
```
