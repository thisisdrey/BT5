### Title
`pallet-psm::redeem` rejects valid redemptions when per‑external‑asset reserve is depleted despite the PSM instance holding sufficient aggregate backing across other approved externals - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
This is a local structural analog of the Ondo `OUSG` Instant Redemption Manager bug: a redemption path checks the balance/backing of a *single* asset bucket instead of the aggregate value actually available to satisfy the request, causing legitimate redemption calls to revert (`InsufficientReserve` / defensive `Unexpected`) even though the PSM instance as a whole is fully solvent.

### Finding Description
`Pallet::redeem` in `substrate/frame/psm/src/lib.rs` burns `internal_asset` and pays out `external_asset` from the PSM's reserve account. The debt and reserve accounting are both scoped **per `(internal_asset, external_asset)` pair**, not per PSM instance: [1](#0-0) 

```
let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

let reserve = Self::get_reserve(&internal_asset, &external_asset);
if reserve < external_out {
    defensive!("PSM reserve is less than expected output amount");
    return Err(Error::<T>::Unexpected.into());
}
```

A single PSM instance (keyed by `internal_asset`) can have *multiple approved external assets* (documented explicitly at [2](#0-1) ), each with its own `ExternalAssets`, `PsmDebt`, and reserve balance held in the *same* `psm_account(&internal_asset)`. `redeem` only inspects the debt/reserve bucket for the `external_asset` requested by the caller — it never looks at whether the PSM's other approved external assets (which back the same fungible `internal_asset`) hold spare value that could satisfy the request.

This is structurally identical to the Ondo finding: the `OUSG` manager tracked `BUIDL` balance as the sole redemption source and reverted when `BUIDL` ran low, even though the manager also held `USDC` that, combined, covered the redemption. Here, `pallet-psm` tracks reserve/debt strictly per external asset id, so if one external asset's bucket is drained by prior redemptions/mints (e.g. `USDC` bucket depleted, `USDT` bucket flush) while the aggregate internal-asset backing across the whole PSM instance remains fully solvent, a user requesting redemption in the depleted external asset is rejected — with `Error::InsufficientReserve`, or in the edge case of an accounting/reserve mismatch, the hard, non-recoverable `defensive!`/`Error::Unexpected` path.

Existing guards do not prevent this because:
- `max_debt` (the debt ceiling, `substrate/frame/psm/src/lib.rs` around line 973) is applied per-instance at mint time but the redemption-time check uses the narrower per-pair `PsmDebt` map, not the aggregate `total_psm_debt` used elsewhere (see `total_psm_debt` usage in `remove_psm`, [3](#0-2) ).
- There is no fallback/aggregation logic that lets `redeem` draw partially from another approved external asset's reserve when the requested one is insufficient — mirroring exactly the missing "concatenate remaining reserve" mitigation Ondo ultimately implemented.

### Impact Explanation
Any unprivileged, signed account calling the public `redeem` extrinsic can be denied a redemption that the PSM instance is economically able to honor, purely due to bucket-level accounting. This is a public-entrypoint availability/fund-access issue: user funds (their `internal_asset` holdings) become temporarily unusable for redemption in the desired external asset, and in the `reserve < external_out` divergence case the call fails via a `defensive!` path that is not intended to be reachable in normal operation, indicating an unhandled state rather than a graceful revert.

### Likelihood Explanation
Any PSM instance configured with more than one approved external asset (an explicitly supported, non-privileged, ordinary configuration per the pallet's own documentation) is subject to this whenever redemption flow naturally skews reserve between external assets — no admin/governance misbehavior, malicious relayer, or validator is required; it emerges purely from normal user mint/redeem activity across multiple externals of the same PSM instance.

### Recommendation
When `redeem` finds the requested `external_asset`'s bucket insufficient, allow the pallet to draw the shortfall from other approved externals of the same PSM instance (or track a single aggregate reserve/debt per `internal_asset` rather than per `(internal_asset, external_asset)` pair), mirroring the mitigation Ondo ultimately adopted (concatenating available balances across asset buckets before rejecting a redemption).

### Proof of Concept
1. Create a PSM for `internal_asset = STABLE` and approve two externals, `USDC` and `USDT`.
2. Alice mints `STABLE` using `USDC` (fills `PsmDebt(STABLE,USDC)` and the psm_account's `USDC` reserve).
3. Bob mints `STABLE` using `USDT` similarly.
4. Alice attempts `redeem(STABLE, USDC, large_amount, ...)` for an amount that exceeds `PsmDebt(STABLE,USDC)`/the `USDC` reserve bucket, even though the PSM's aggregate backing (USDC+USDT reserves) would be more than sufficient to cover it.
5. The call reverts with `Error::InsufficientReserve` (or, if reserve and debt bookkeeping diverge, `Error::Unexpected` via the `defensive!` branch), matching the existing test `fails_insufficient_reserve` / `fails_when_reserve_exceeds_debt_donated_reserves` in [4](#0-3) , which the pallet's own test suite already demonstrates as expected (rejecting) behavior — confirming redemption fails purely due to per-bucket accounting rather than actual aggregate solvency.

Note: I was unable to fully verify whether any downstream runtime configuration of `pallet-psm` (in this repository) actually deploys multiple external assets per instance in production, since only pallet-level code and tests were indexed; this would need to be confirmed in a live Devin session with full repository access.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L37-40)
```rust
//! * **External** — third-party assets (e.g. USDC, USDT) approved on a specific PSM via
//!   [`Pallet::add_external_asset`] and held in that PSM's reserve. Users deposit external to mint
//!   internal, and burn internal to redeem external. A PSM may approve multiple externals, each
//!   identified by `external_asset`.
```

**File:** substrate/frame/psm/src/lib.rs (L848-855)
```rust
			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}
```

**File:** substrate/frame/psm/src/lib.rs (L1032-1034)
```rust
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;
			ensure!(info.external_count == 0, Error::<T>::PsmHasApprovedExternals);
			ensure!(Self::total_psm_debt(&internal_asset).is_zero(), Error::<T>::PsmHasDebt);
```

**File:** substrate/frame/psm/src/tests.rs (L595-719)
```rust
	#[test]
	fn fails_insufficient_reserve() {
		new_test_ext().execute_with(|| {
			fund_internal(BOB, 10_000 * INTERNAL_UNIT);

			let reserve = get_asset_balance(USDC_ASSET_ID, psm_account());
			assert_eq!(reserve, 0);

			assert_noop!(
				Psm::redeem(
					RuntimeOrigin::signed(BOB),
					INTERNAL_ASSET_ID,
					USDC_ASSET_ID,
					1000 * INTERNAL_UNIT,
					Permill::from_percent(1)
				),
				Error::<Test>::InsufficientReserve
			);
		});
	}

	#[test]
	fn fails_insufficient_internal_balance() {
		ExtBuilder::default()
			.mints(ALICE, 5000 * INTERNAL_UNIT)
			.mints(BOB, 10_000 * INTERNAL_UNIT)
			.build_and_execute(|| {
				let alice_internal = get_asset_balance(INTERNAL_ASSET_ID, ALICE);
				let too_much = alice_internal + 1000 * INTERNAL_UNIT;

				assert_noop!(
					Psm::redeem(
						RuntimeOrigin::signed(ALICE),
						INTERNAL_ASSET_ID,
						USDC_ASSET_ID,
						too_much,
						Permill::from_percent(1)
					),
					TokenError::FundsUnavailable
				);
			});
	}

	#[test]
	fn boundary_reserve_equals_output() {
		new_test_ext().execute_with(|| {
			set_minting_fee(USDC_ASSET_ID, Permill::zero());
			set_redemption_fee(USDC_ASSET_ID, Permill::zero());

			let amount = 5000 * INTERNAL_UNIT;
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				amount,
				Permill::zero()
			));
			assert_ok!(Psm::redeem(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				amount,
				Permill::zero()
			));

			assert_eq!(get_asset_balance(USDC_ASSET_ID, psm_account()), 0);
		});
	}

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

			// Verify boundary: exactly debt works, but debt+1 does not
			hypothetically!({
				assert_ok!(Psm::redeem(
					RuntimeOrigin::signed(ALICE),
					INTERNAL_ASSET_ID,
					USDC_ASSET_ID,
					debt,
					Permill::zero()
				));
				assert_eq!(get_asset_balance(USDC_ASSET_ID, psm_account()), donation);
			});

			assert_noop!(
				Psm::redeem(
					RuntimeOrigin::signed(ALICE),
					INTERNAL_ASSET_ID,
					USDC_ASSET_ID,
					debt + 1,
					Permill::zero()
				),
				Error::<Test>::InsufficientReserve
			);
		});
	}
}
```
