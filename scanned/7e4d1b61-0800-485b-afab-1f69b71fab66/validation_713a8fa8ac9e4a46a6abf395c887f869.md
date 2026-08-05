Confirmed: `payout()` pays out `spend.amount` in `spend.asset_kind` units directly via `T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)` [1](#0-0) , never re-checking `BalanceConverter`/`AssetRate` at payout time. The only place the asset→native conversion rate is consulted is once, at `spend()` time, purely to gate the `SpendOrigin`'s authorized ceiling.

### Title
Stale/ungoverned `AssetRate` conversion lets a capped `SpendOrigin` authorize treasury spends worth far more than its permitted native-equivalent ceiling - (File: substrate/frame/treasury/src/lib.rs)

### Summary
`pallet-treasury::spend()` enforces a capped-origin's spending limit by converting an arbitrary `asset_kind`/`amount` pair into a native-currency equivalent through `Config::BalanceConverter` (backed in practice by `pallet-asset-rate`), and only allows the spend if that computed `native_amount <= max_amount` [2](#0-1) . This is the exact analog of the external report's core broken invariant: a price/rate value is consumed to gate a financial operation (mint/redeem there, spend-authorization here) with no freshness or market-consistency check on that rate.

### Finding Description
`AssetRate::from_asset_balance` simply reads a `FixedU128` value from `ConversionRateToNative` storage and multiplies it by the requested balance — there is no timestamp, heartbeat, or staleness bound on this value at all [3](#0-2) . The rate is set once via `create`/`update` extrinsics and then persists indefinitely until someone calls `update` again [4](#0-3) .

`Treasury::spend()` uses this potentially-stale rate as the *sole* gate on how much value a bounded `SpendOrigin` (e.g. a track with `Success = 100 DOT`) may authorize:
```
let native_amount = T::BalanceConverter::from_asset_balance(amount, *asset_kind.clone())...
ensure!(native_amount <= max_amount, Error::<T, I>::InsufficientPermission);
``` [5](#0-4) 

Crucially, the `amount`/`asset_kind` pair that is actually recorded and later paid out is **never re-derived from native value** — `payout()` pays exactly `spend.amount` of `spend.asset_kind` through the configured `Paymaster` [1](#0-0) , up to `PayoutPeriod` later (30 days in most runtime configs, e.g. `PayoutSpendPeriod` in `polkadot/runtime/rococo/src/lib.rs`) [6](#0-5) .

The broken invariant: **the value gate (`AssetRate`) and the value actually transferred (`asset_kind`/`amount` via `Paymaster`) are decoupled and never reconciled.** If the on-chain `ConversionRateToNative[asset_kind]` understates the asset's real market value relative to native currency (because it is stale, was set once at listing time, or the asset's real price has since diverged — exactly the Chainlink-staleness scenario in the external report, just moved on-chain and manually curated instead of oracle-fed), then a `SpendOrigin` with a small native-equivalent budget (e.g. 100 DOT) can `spend()` an `amount` of `asset_kind` that is nominally "worth" 100 DOT per the stale rate, but is actually worth far more in real terms. The `payout()` call later transfers the full real-value amount of `asset_kind` to the beneficiary, with no re-check against `max_amount` or any updated rate.

This mirrors the report's exploit structure precisely: mint against a stale low price, then redeem/settle at the real (higher) price to extract value beyond what the gating mechanism intended to permit.

### Impact Explanation
This allows unbacked/over-authorized settlement of value from the Treasury: a spend-limited origin can cause the pallet to commit (and later, via `payout`, irrevocably transfer) treasury-held assets whose real value exceeds the origin's sanctioned ceiling, solely because the conversion oracle (`AssetRate`) is stale relative to the asset's true worth. This is a "theft/unbacked… payout to wrong amount" and "runtime bug compromising intended behavior" class impact under the program's own inclusion criteria — the enforced *invariant* ("this origin may authorize at most X native-equivalent value") silently fails to hold once the asset/native rate drifts, and the protocol has no mechanism to detect or reject that drift before committing/paying the spend.

### Likelihood Explanation
`pallet-asset-rate` documents explicitly that "conversion rates are only used as estimates, and are not designed to be precise or closely tracking real world values" [7](#0-6) , i.e., staleness/inaccuracy is an accepted, expected, and persistent property of this input — not a rare edge case. Any runtime enabling non-root, budget-capped `SpendOrigin` tracks together with a non-native `AssetKind` (as configured in `substrate/bin/node/runtime/src/lib.rs` and the AssetHub/parachain governance configs shown) is exposed as soon as the market value of any approved `AssetKind` moves away from its registered rate, which requires no attacker privilege beyond already holding a capped spend-origin permission that the system intends to bound.

### Recommendation
- Re-validate (or re-derive) the native-equivalent value of a pending spend at `payout()` time using the current `BalanceConverter`/`AssetRate`, and reject/require re-approval if it now exceeds the amount authorized at `spend()` time.
- Alternatively, record the native-equivalent amount (not just `asset_kind`/`amount`) at `spend()` time, and cap the real transferred value to that recorded native-equivalent ceiling regardless of later rate changes.
- Add a staleness/last-updated marker to `pallet-asset-rate` (analogous to Chainlink's `updatedAt`) and require callers of `from_asset_balance` in security-sensitive contexts (like `Treasury::spend`) to reject rates older than a configurable threshold.

### Proof of Concept
1. Governance (via `AssetRate::create`) sets `ConversionRateToNative[AssetX] = 0.01` (i.e., 1 unit of AssetX ≈ 0.01 native), reflecting AssetX's price at listing time.
2. Time passes; AssetX's real market value rises 100x, but no one calls `AssetRate::update` (staleness — nobody is obligated to keep it current, matching the "oracle stops updating" premise of the external report).
3. A capped `SpendOrigin` track with `Success = 100` native units calls `Treasury::spend(AssetX, 10_000, beneficiary, None)`.
4. `from_asset_balance(10_000, AssetX)` computes `10_000 * 0.01 = 100 <= 100`, so `ensure!` passes and the spend is approved [5](#0-4) .
5. At real market price, `10_000` units of AssetX are actually worth `10,000` native-equivalent — 100x the origin's authorized ceiling.
6. Beneficiary calls `payout(index)`; `Paymaster::pay` transfers the full `10_000` AssetX to the beneficiary, unconditionally [1](#0-0) , realizing value far beyond what the `SpendOrigin` was ever entitled to authorize.

### Citations

**File:** substrate/frame/treasury/src/lib.rs (L658-670)
```rust
			let max_amount = T::SpendOrigin::ensure_origin(origin)?;
			let beneficiary = T::BeneficiaryLookup::lookup(*beneficiary)?;

			let now = T::BlockNumberProvider::current_block_number();
			let valid_from = valid_from.unwrap_or(now);
			let expire_at = valid_from.saturating_add(T::PayoutPeriod::get());
			ensure!(expire_at > now, Error::<T, I>::SpendExpired);

			let native_amount =
				T::BalanceConverter::from_asset_balance(amount, *asset_kind.clone())
					.map_err(|_| Error::<T, I>::FailedToConvertBalance)?;

			ensure!(native_amount <= max_amount, Error::<T, I>::InsufficientPermission);
```

**File:** substrate/frame/treasury/src/lib.rs (L747-748)
```rust
			let id = T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)
				.map_err(|_| Error::<T, I>::PayoutError)?;
```

**File:** substrate/frame/asset-rate/src/lib.rs (L54-57)
```rust
//! ### Assumptions
//!
//! * Conversion rates are only used as estimates, and are not designed to be precise or closely
//!   tracking real world values.
```

**File:** substrate/frame/asset-rate/src/lib.rs (L185-216)
```rust
		/// Update the conversion rate to native balance for the given asset.
		///
		/// ## Complexity
		/// - O(1)
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::update())]
		pub fn update(
			origin: OriginFor<T>,
			asset_kind: Box<T::AssetKind>,
			rate: FixedU128,
		) -> DispatchResult {
			T::UpdateOrigin::ensure_origin(origin)?;

			let mut old = FixedU128::zero();
			ConversionRateToNative::<T>::mutate(asset_kind.as_ref(), |maybe_rate| {
				if let Some(r) = maybe_rate {
					old = *r;
					*r = rate;

					Ok(())
				} else {
					Err(Error::<T>::UnknownAssetKind)
				}
			})?;

			Self::deposit_event(Event::AssetRateUpdated {
				asset_kind: *asset_kind,
				old,
				new: rate,
			});
			Ok(())
		}
```

**File:** substrate/frame/asset-rate/src/lib.rs (L246-253)
```rust
	fn from_asset_balance(
		balance: BalanceOf<T>,
		asset_kind: AssetKindOf<T>,
	) -> Result<BalanceOf<T>, pallet::Error<T>> {
		let rate = pallet::ConversionRateToNative::<T>::get(asset_kind)
			.ok_or(pallet::Error::<T>::UnknownAssetKind.into())?;
		Ok(rate.saturating_mul_int(balance))
	}
```

**File:** polkadot/runtime/rococo/src/lib.rs (L537-571)
```rust
impl pallet_treasury::Config for Runtime {
	type PalletId = TreasuryPalletId;
	type Currency = Balances;
	type RejectOrigin = EitherOfDiverse<EnsureRoot<AccountId>, Treasurer>;
	type RuntimeEvent = RuntimeEvent;
	type SpendPeriod = SpendPeriod;
	type Burn = Burn;
	type BurnDestination = Society;
	type MaxApprovals = MaxApprovals;
	type WeightInfo = weights::pallet_treasury::WeightInfo<Runtime>;
	type SpendFunds = Bounties;
	type SpendOrigin = TreasurySpender;
	type AssetKind = VersionedLocatableAsset;
	type Beneficiary = VersionedLocation;
	type BeneficiaryLookup = IdentityLookup<Self::Beneficiary>;
	type Paymaster = PayOverXcm<
		TreasuryInteriorLocation,
		crate::xcm_config::XcmConfig,
		crate::XcmPallet,
		ConstU32<{ 6 * HOURS }>,
		Self::Beneficiary,
		Self::AssetKind,
		LocatableAssetConverter,
		VersionedLocationConverter,
	>;
	type BalanceConverter = UnityOrOuterConversion<
		ContainsParts<
			FromContains<
				xcm_builder::IsChildSystemParachain<ParaId>,
				xcm_builder::IsParentsOnly<ConstU8<1>>,
			>,
		>,
		AssetRate,
	>;
	type PayoutPeriod = PayoutSpendPeriod;
```
