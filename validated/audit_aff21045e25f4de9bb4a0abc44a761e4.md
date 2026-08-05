Audit Report

## Title
Asymmetric decimals-drift guard in `pallet-psm`: `redeem` silently uses a stale decimals snapshot while `mint` halts, breaking the pallet's 1:1 peg invariant - (File: substrate/frame/psm/src/lib.rs)

## Summary
`pallet-psm` snapshots the internal asset's decimals at PSM-creation time in `PsmInfo` and uses this snapshot (together with the external asset's snapshot) to compute conversion factors via `external_to_internal`/`internal_to_external`, which the pallet's own documentation says must be validated against live decimals "on every swap." The `mint` path enforces this check and reverts with `DecimalsMismatch` when the internal asset's live decimals diverge from the snapshot, but the `redeem` path performs no equivalent check and settles real balances using the stale snapshot decimals as the scaling factor.

## Finding Description
The conversion factor between external and internal balances is derived purely from the decimals difference in `external_to_internal`/`internal_to_external` [1](#0-0) . The pallet's documented invariant is that "the internal asset's live decimals must still match the snapshot in `PsmInfo`" and that this is validated "on every swap" [2](#0-1) . Decimals are owner-controlled and mutable at any time via `pallet_assets::set_metadata`/`force_set_metadata`, which is gated only by asset ownership, not by any PSM-level authorization [3](#0-2) .

The pallet's own test suite demonstrates the asymmetry directly: `mint_halts_when_internal_decimals_drift` shows `mint` reverting with `Error::DecimalsMismatch` once the internal asset's owner changes its live decimals after PSM registration, while `redeem_uses_snapshot_when_internal_decimals_drift` shows `redeem`, under the identical drift condition, proceeding to completion and moving real `USDC`, `INTERNAL`, `INSURANCE_FUND`, and `PsmDebt` balances computed from the stale, pre-drift decimals snapshot [4](#0-3) . This confirms `pallet-psm` contains a decimals-drift check that is applied only to one of the two symmetric swap directions, exactly as described in the claim.

## Impact Explanation
Decimals are the pricing multiplier for this 1:1-peg reserve module. Because `mint` treats live-decimals drift as fatal (correctly halting new debt issuance against a possibly mispriced asset) while `redeem` does not, any user can continue draining the PSM's external reserve (`get_reserve`) at the frozen `InternalDecimals`/`StableDecimals` snapshot rate even after the pallet's own mint-side logic has determined that rate is untrustworthy. This is a runtime bug that compromises the pallet's intended peg-parity behavior and can result in redemption payouts inconsistent with the actual value backing the internal asset, i.e., an economically stale settlement that mis-prices real reserve funds.

## Likelihood Explanation
The only capability required is that of the internal asset's owner (or a party the owner grants `force_set_metadata`-adjacent permission to), an ordinary, unprivileged `pallet_assets` capability that is entirely independent from PSM governance or validator/collator authority. No governance action, validator collusion, or off-chain infrastructure control is needed — a normal `set_metadata` call followed by a normal `redeem` extrinsic reproduces the issue deterministically, as demonstrated by the pallet's own passing test `redeem_uses_snapshot_when_internal_decimals_drift`.

## Recommendation
Apply the same live-vs-snapshot decimals check used in `mint` (yielding `DecimalsMismatch`) to `redeem` and any other function that invokes `external_to_internal`/`internal_to_external`, so both swap directions halt consistently whenever live decimals diverge from the value recorded in `PsmInfo` at registration time.

## Proof of Concept
1. Create a PSM instance for `INTERNAL_ASSET_ID` (decimals snapshot = 6) with `USDC_ASSET_ID` approved as external.
2. Perform a normal `Psm::mint` to seed balances, then have the internal asset owner (`ALICE`) call `Assets::set_metadata(INTERNAL_ASSET_ID, ..., decimals = 8)` to drift live decimals away from the snapshot.
3. Observe `Psm::mint(...)` now reverts with `Error::DecimalsMismatch`, per `mint_halts_when_internal_decimals_drift`.
4. Observe `Psm::redeem(...)` for the same asset still succeeds and settles `USDC`, `INTERNAL`, `INSURANCE_FUND`, and `PsmDebt` balances using the stale snapshot decimals, per `redeem_uses_snapshot_when_internal_decimals_drift` [4](#0-3) .

### Citations

**File:** substrate/frame/psm/src/lib.rs (L1580-1599)
```rust
		pub(crate) fn external_to_internal(
			amount: BalanceOf<T>,
			ext_decimals: u8,
			internal_decimals: u8,
		) -> Result<BalanceOf<T>, Error<T>> {
			use core::cmp::Ordering::*;
			match ext_decimals.cmp(&internal_decimals) {
				Equal => Ok(amount),
				Less => {
					let diff = (internal_decimals - ext_decimals) as u32;
					let factor = Self::pow10(diff)?;
					amount.checked_mul(&factor).ok_or(Error::<T>::ConversionOverflow)
				},
				Greater => {
					let diff = (ext_decimals - internal_decimals) as u32;
					let factor = Self::pow10(diff)?;
					Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
				},
			}
		}
```

**File:** substrate/frame/psm/README.md (L146-180)
```markdown
### Asset Onboarding Requirements

Before calling `add_external_asset(internal_asset, asset_id)`:

- A PSM must already be registered for `internal_asset`
- The external `asset_id` must already exist in the `Fungibles` implementation
- The internal asset's live decimals must still match the snapshot in `PsmInfo`
- `|external_decimals − internal_decimals|` must be within `MAX_DECIMALS_DIFF`
- The PSM must still be below `MaxExternals`

After `add_external_asset`, the external starts with an `AssetCeilingWeight` of `0%`, so its
per-external ceiling is zero and **minting is disabled**. Before the first mint, call
`set_asset_ceiling_weight(internal_asset, asset_id, weight)` with a non-zero weight (and
optionally `set_minting_fee` / `set_redemption_fee`, which otherwise default to 0.5%).
Skipping this step makes the first mint fail with `ExceedsMaxPsmDebt`.

## Configuration

```rust
impl pallet_psm::Config for Runtime {
    type Fungibles = Assets;
    type Currency = Balances;
    type RuntimeOrigin = RuntimeOrigin;
    type PalletsOrigin = OriginCaller;
    type AssetId = u32;
    type WeightInfo = weights::SubstrateWeight<Runtime>;
    type PalletId = PsmPalletId;
    type MaxExternals = ConstU32<10>;
    type CreationDeposit = PsmCreationDeposit;
}
```

`Fungibles` must expose metadata for both internal and external assets, because
`add_external_asset` snapshots the external's decimals and the pallet validates
on every swap that live decimals still match.
```

**File:** substrate/frame/assets/src/functions.rs (L1058-1093)
```rust
	/// Do set metadata
	pub(super) fn do_set_metadata(
		id: T::AssetId,
		from: &T::AccountId,
		name: Vec<u8>,
		symbol: Vec<u8>,
		decimals: u8,
	) -> DispatchResult {
		let bounded_name: BoundedVec<u8, T::StringLimit> =
			name.clone().try_into().map_err(|_| Error::<T, I>::BadMetadata)?;
		let bounded_symbol: BoundedVec<u8, T::StringLimit> =
			symbol.clone().try_into().map_err(|_| Error::<T, I>::BadMetadata)?;

		let d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(d.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);
		ensure!(from == &d.owner, Error::<T, I>::NoPermission);

		Metadata::<T, I>::try_mutate_exists(id.clone(), |metadata| {
			ensure!(metadata.as_ref().map_or(true, |m| !m.is_frozen), Error::<T, I>::NoPermission);

			let old_deposit = metadata.take().map_or(Zero::zero(), |m| m.deposit);
			let new_deposit = Self::calc_metadata_deposit(&name, &symbol);

			if new_deposit > old_deposit {
				T::Currency::reserve(from, new_deposit - old_deposit)?;
			} else {
				T::Currency::unreserve(from, old_deposit - new_deposit);
			}

			*metadata = Some(AssetMetadata {
				deposit: new_deposit,
				name: bounded_name,
				symbol: bounded_symbol,
				decimals,
				is_frozen: false,
			});
```

**File:** substrate/frame/psm/src/tests.rs (L3030-3103)
```rust
	#[test]
	fn mint_halts_when_internal_decimals_drift() {
		new_test_ext().execute_with(|| {
			// internal starts at 6 decimals; InternalDecimals snapshot matches. The owner
			// (ALICE) changes the internal asset's live metadata to simulate drift.
			assert_ok!(Assets::set_metadata(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				b"INTERNAL".to_vec(),
				b"INTERNAL".to_vec(),
				8
			));

			assert_noop!(
				Psm::mint(
					RuntimeOrigin::signed(ALICE),
					INTERNAL_ASSET_ID,
					USDC_ASSET_ID,
					1000 * INTERNAL_UNIT,
					Permill::from_percent(1)
				),
				Error::<Test>::DecimalsMismatch
			);
		});
	}

	#[test]
	fn redeem_uses_snapshot_when_internal_decimals_drift() {
		new_test_ext().execute_with(|| {
			// Seed ALICE's internal balance and PSM reserve with a prior mint, then
			// drift the internal asset's decimals.
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				1000 * INTERNAL_UNIT,
				Permill::from_percent(1)
			));
			assert_ok!(Assets::set_metadata(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				b"INTERNAL".to_vec(),
				b"INTERNAL".to_vec(),
				8
			));

			let alice_usdc_before = get_asset_balance(USDC_ASSET_ID, ALICE);
			let alice_internal_before = get_asset_balance(INTERNAL_ASSET_ID, ALICE);
			let insurance_internal_before = get_asset_balance(INTERNAL_ASSET_ID, INSURANCE_FUND);
			let debt_before = PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID);
			let redeem = 100 * INTERNAL_UNIT;
			let fee = Permill::from_percent(1).mul_ceil(redeem);
			let external_out = redeem - fee;

			assert_ok!(Psm::redeem(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				redeem,
				Permill::from_percent(1)
			));

			assert_eq!(get_asset_balance(USDC_ASSET_ID, ALICE), alice_usdc_before + external_out);
			assert_eq!(get_asset_balance(INTERNAL_ASSET_ID, ALICE), alice_internal_before - redeem);
			assert_eq!(
				get_asset_balance(INTERNAL_ASSET_ID, INSURANCE_FUND),
				insurance_internal_before + fee
			);
			assert_eq!(
				PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID),
				debt_before - external_out
			);
		});
	}
```
