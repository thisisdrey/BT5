Based on my investigation, I found a strong local analog in `substrate/frame/psm` (Peg Stability Module pallet), which mirrors the core broken invariant from the Pendle report: an assumption that value converts 1:1 (or via a fixed, snapshotted rate) between an internal accounting unit and an external asset, when the actual external asset's real-world value/decimals can diverge from that assumption over time.

### Title
Stale decimals snapshot in PSM external-asset registration can break internal/external conversion invariants, allowing under/over-collateralized redemptions - (File: substrate/frame/psm/src/lib.rs)

### Summary
The PSM pallet documents that "Redemptions use the decimals snapshotted when the PSM/external pair was registered, allowing existing positions to unwind even if live metadata later changes" [1](#0-0) . This is structurally the same broken assumption as the Pendle bug: the code hard-codes a fixed conversion factor (decimals snapshot) between two asset units and assumes it always remains valid, rather than re-deriving it from the live state of the external asset.

### Finding Description
`redeem` and `mint` convert between `internal_asset` and `external_asset` using `internal_decimals` and `ext_decimals` fields cached in `PsmInfo`/`ExternalAssets` storage at registration time [2](#0-1) . The debt ceiling and reserve accounting (`PsmDebt`) are all derived using this fixed decimals-based scaling via `internal_to_external`/`external_to_internal` [3](#0-2) . Just as the Pendle oracle assumed `SY == YieldToken` and used a fixed rate lookup without accounting for real redemption slippage, the PSM pallet assumes the external asset's decimal/value relationship to the internal stablecoin remains exactly what was snapshotted at registration, with no live re-validation. If the external asset's metadata (decimals) is later reconfigured by its issuing pallet, or if the external asset is not actually a true 1:1-pegged stable asset (e.g., a rebasing or fee-on-transfer asset), the PSM will systematically over- or under-value redemptions relative to real backing, since `PsmDebt` tracks a purely arithmetic value rather than actual reserve worth.

### Impact Explanation
If the snapshot decimals diverge from the live/true value ratio of the external asset, users could redeem more external-asset value than their internal-asset burn actually represents, draining the PSM reserve for other users and mirroring the "collateral overvaluation → protocol insolvency" impact pattern from the Pendle report. This is a value-conservation break in an asset/treasury-accounting pallet, matching the required-impact category of "theft or unbacked mint or unlock" / conservation-of-value violations.

### Likelihood Explanation
Medium — this requires either (a) an asset's real decimals/value characteristics changing after PSM registration (e.g., asset issuer/registrar changing metadata), or (b) an external asset being added that is not truly 1:1 with the internal stablecoin (a PSM admin/config error) but treated as such by the fixed decimals-snapshot model. The pallet's own reserve-vs-debt invariant tests (`fails_when_reserve_exceeds_debt_donated_reserves`) confirm redemption is gated by `PsmDebt`, not real reserve value, so if `PsmDebt` accounting is wrong relative to true backing, the mismatch is not otherwise caught [4](#0-3) .

### Recommendation
Re-validate or re-derive the external asset's decimals/value characteristics at redemption time (or restrict PSM-eligible externals to assets whose decimals/backing are provably immutable), and add an invariant check comparing tracked `PsmDebt` against externally-verifiable reserve value rather than relying solely on the registration-time snapshot.

### Proof of Concept
Not independently reproducible from the index alone — this requires exercising the pallet with an external asset whose decimals are changed after PSM registration (or documenting that such change is currently possible via the external asset's own management pallet), which I could not fully confirm from the available file excerpts within this investigation's scope. This should be validated further by a follow-up session with full file access to `substrate/frame/psm/src/lib.rs` (e.g., `add_external_asset`, `ExternalAssets` storage definition, and the asset-metadata source used for `ext_decimals`) to confirm whether decimals can indeed be mutated post-registration on the underlying asset object while the PSM snapshot remains stale.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L780-783)
```rust
		/// calculated using ceiling rounding (`mul_ceil`), ensuring the protocol never
		/// undercharges. Redemptions use the decimals snapshotted when the PSM/external pair
		/// was registered, allowing existing positions to unwind even if live metadata later
		/// changes.
```

**File:** substrate/frame/psm/src/lib.rs (L811-827)
```rust
		pub fn redeem(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			internal_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;

```

**File:** substrate/frame/psm/src/lib.rs (L835-849)
```rust
			let external_out =
				Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
			ensure!(
				internal_net.is_zero() || !external_out.is_zero(),
				Error::<T>::AmountTooSmallAfterConversion
			);
			// `effective_internal_net` is the internal value that round-trips to `external_out`;
			// it is what we actually burn and what the tracked debt decreases by. Any truncation
			// dust stays in the caller's internal balance, symmetric with `mint`, which takes
			// only the round-tripped share of the external amount.
			let effective_internal_net =
				Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);
```

**File:** substrate/frame/psm/src/tests.rs (L664-693)
```rust
	#[test]
	fn fails_when_reserve_exceeds_debt_donated_reserves() {
		ExtBuilder::default().mints(ALICE, 5000 * INTERNAL_UNIT).build_and_execute(|| {
			set_redemption_fee(USDC_ASSET_ID, Permill::zero());

			let debt = PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID);
			let donation = 5000 * INTERNAL_UNIT;

			// Defensive path: simulate donated reserves by funding psm_account()
			// directly, bypassing mint to create a reserve > debt scenario.
			fund_external_asset(USDC_ASSET_ID, psm_account(), donation);

			let reserve = get_asset_balance(USDC_ASSET_ID, psm_account());
			assert!(reserve > debt, "reserve should exceed debt after donation");

			// Give user enough internal to try redeeming more than debt
			let redeem_amount = debt + donation;
			fund_internal(ALICE, redeem_amount);

			// Should fail because redemption is limited by debt, not reserve
			assert_noop!(
				Psm::redeem(
					RuntimeOrigin::signed(ALICE),
					INTERNAL_ASSET_ID,
					USDC_ASSET_ID,
					redeem_amount,
					Permill::zero()
				),
				Error::<Test>::InsufficientReserve
			);
```
