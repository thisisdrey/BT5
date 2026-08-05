### Title
`Treasury::spend` gates a `SpendOrigin`'s native-value spending limit on an unbounded, staleness-unchecked `AssetRate::ConversionRateToNative` value - ([File: substrate/frame/treasury/src/lib.rs])

### Summary
`pallet-treasury`'s `spend` extrinsic converts an `asset_kind`/`amount` pair into an equivalent native-currency amount purely to check it against the calling `SpendOrigin`'s authorized ceiling (`max_amount`). The conversion is delegated to `pallet-asset-rate`'s `ConversionRateToNative` storage value via `ConversionFromAssetBalance::from_asset_balance`, exactly as `MCAGRateFeed#getRate` fed a price into KUMASwap's bond-pricing logic. Just like the Solidity oracle, `pallet-asset-rate` records only a raw `FixedU128` rate with no timestamp, no "last updated" field, and no consumer-side freshness check.

### Finding Description
`pallet_asset_rate::ConversionRateToNative` is a plain `StorageMap<AssetKind, FixedU128>` with no timestamp metadata at all: [1](#0-0) 

`from_asset_balance` simply reads whatever rate is currently stored and multiplies: [2](#0-1) 

`Treasury::spend` uses this rate solely to authorize how much native-equivalent value a `SpendOrigin` is permitted to allocate: [3](#0-2) 

The stored spend record keeps only the raw `asset_kind`/`amount`, not the native value that was checked at approval time: [4](#0-3) 

and `payout` pays out the *asset-denominated* `amount` unconditionally, without re-deriving or re-checking the native equivalent at payout time: [5](#0-4) 

This is structurally identical to the reported bug class: a value (`answer`/`rate`) is read from a single storage slot with no freshness metadata and used to authorize downstream financial decisions, without any bound on how long ago that value was set. `pallet_asset_rate::update` only changes the rate when an `UpdateOrigin` proactively calls it; there is no heartbeat, no `updated_at` block number, and no mechanism forcing periodic refresh (mirroring KUMA's `oracle.latestRoundData()` call that never checked `updatedAt`).

Concretely: a `SpendOrigin` instance is only authorized up to `max_amount` in native terms (e.g. `EnsureWithSuccess<..., MaxBalance>` grants unlimited spend for root, but lower-tier origins such as `Spender`/`Treasurer` are capped). The cap is enforced only through the stale `ConversionRateToNative` snapshot. If the real relative value of `asset_kind` has moved since the rate was last updated (which can be an arbitrarily long time, since there is no enforced refresh cadence), the `native_amount` computed at `spend()`-time can systematically understate the true value being allocated. This lets an origin with a bounded, lower-tier spending permission (e.g., a `Treasurer`/`Spender` track that intentionally has a smaller ceiling than root) push through `spend()` calls that, at real-world value, exceed what governance intended to authorize for that origin tier — an origin-authorization bypass rooted in unchecked stale valuation data, not in any admin/governance misuse (the bug is the *absence* of a staleness check in `pallet-asset-rate`/`pallet-treasury`, exactly as in the original finding).

### Impact Explanation
This falls under "unauthorized execution or origin escalation" and "treasury spends ... must conserve value and settle exactly once to the rightful beneficiary and amount": a spend-tier origin can, via a stale conversion rate that nobody is forced to refresh, authorize the treasury to commit more real value than the origin's tier was designed to permit, effectively escalating its spending authority beyond the intended ceiling. Because `payout` transfers the raw `asset_kind` amount (not a native-adjusted amount), any staleness in the rate directly determines how much real value slips past the permission check.

### Likelihood Explanation
Likelihood is moderate: it does not require a malicious relayer, governance actor, or admin — the vulnerability is the missing invariant itself. Any asset whose value fluctuates relative to native currency and whose `ConversionRateToNative` entry is not proactively kept current (which the code does nothing to enforce) will silently drift, and any legitimately-scoped `SpendOrigin` (not just root) can exploit the drift purely by calling the public `spend` extrinsic when the rate is favorably stale. The severity/likelihood depends on how volatile the given `AssetKind` is and how infrequently `update`/`create` is called, mirroring KUMA's own acknowledgment that infrequent updates make the missing check practically exploitable during any window of drift.

### Recommendation
Add timestamp/block-number metadata to `pallet_asset_rate::ConversionRateToNative` (e.g. store `(FixedU128, BlockNumberFor<T>)`), and have `Treasury::spend`/`ConversionFromAssetBalance::from_asset_balance` reject (or require re-validation of) rates older than a configurable `MaxRateAge`. Alternatively, re-derive and re-check the native equivalent at `payout` time (not just at `spend` approval time) against the origin's original permission ceiling, so a drifted rate cannot be exploited between approval and payout, and so no rate can be used indefinitely without being refreshed.

### Proof of Concept
1. Governance grants a `Treasurer`/`Spender`-tier `SpendOrigin` a native-equivalent ceiling of `X` DOT via `TreasurySpender`.
2. `AssetRate::create(asset_kind, rate=R0)` is set once and never subsequently updated (no code path forces refresh).
3. Over time, the real market value of `asset_kind` relative to native currency increases substantially, but `ConversionRateToNative` still reports `R0`.
4. The `Treasurer` origin calls `Treasury::spend(asset_kind, amount, beneficiary, None)` with an `amount` such that `R0 * amount <= X` (passes the `ensure!(native_amount <= max_amount, ...)` check at line 670 of `substrate/frame/treasury/src/lib.rs`), even though the *actual* value of `amount` of `asset_kind` significantly exceeds `X`.
5. `Treasury::payout` later pays out the full `amount` of `asset_kind` to `beneficiary` — real value transferred exceeds what the `Treasurer` tier was authorized to commit, with no additional check at payout time.

### Citations

**File:** substrate/frame/asset-rate/src/lib.rs (L132-137)
```rust
	/// Maps an asset to its fixed point representation in the native balance.
	///
	/// E.g. `native_amount = asset_amount * ConversionRateToNative::<T>::get(asset_kind)`
	#[pallet::storage]
	pub type ConversionRateToNative<T: Config> =
		StorageMap<_, Blake2_128Concat, T::AssetKind, FixedU128, OptionQuery>;
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

**File:** substrate/frame/treasury/src/lib.rs (L690-701)
```rust
			let index = SpendCount::<T, I>::get();
			Spends::<T, I>::insert(
				index,
				SpendStatus {
					asset_kind: *asset_kind.clone(),
					amount,
					beneficiary: beneficiary.clone(),
					valid_from,
					expire_at,
					status: PaymentState::Pending,
				},
			);
```

**File:** substrate/frame/treasury/src/lib.rs (L736-757)
```rust
		pub fn payout(origin: OriginFor<T>, index: SpendIndex) -> DispatchResult {
			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now >= spend.valid_from, Error::<T, I>::EarlyPayout);
			ensure!(spend.expire_at > now, Error::<T, I>::SpendExpired);
			ensure!(
				matches!(spend.status, PaymentState::Pending | PaymentState::Failed),
				Error::<T, I>::AlreadyAttempted
			);

			let id = T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)
				.map_err(|_| Error::<T, I>::PayoutError)?;

			spend.status = PaymentState::Attempted { id };
			spend.expire_at = now.saturating_add(T::PayoutPeriod::get());
			Spends::<T, I>::insert(index, spend);

			Self::deposit_event(Event::<T, I>::Paid { index, payment_id: id });

			Ok(())
		}
```
