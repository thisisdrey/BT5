### Title
Donated / Accidentally-Transferred Reserve Tokens Are Permanently Locked In `pallet-psm` Reserve Accounts - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm`'s per-instance reserve account tracks how much external-asset liquidity users may redeem via the separate `PsmDebt` storage item, not via the reserve account's actual token balance. Any external-asset tokens that end up in the reserve account beyond what `PsmDebt` recorded (e.g. a direct/accidental transfer, or intentional "donation" to the reserve) are structurally unrecoverable: `redeem` is capped by `PsmDebt`, and there is no admin extrinsic to sweep or reclaim the excess. This mirrors the Shelter.sol pattern in the reference report — a separately-tracked accounting variable (`amountInShelter` / `PsmDebt`) diverges from the real token balance, and the excess becomes permanently stranded with no recovery path.

### Finding Description
Redemption in `pallet-psm` is explicitly limited by the tracked debt counter, not the raw reserve balance:

> "Limited by the per-external tracked debt (`PsmDebt`), not raw reserve balance" [1](#0-0) 

The pallet's own test suite confirms this divergence is reachable and produces a stuck-fund state: tokens transferred directly into the PSM reserve account (bypassing `mint`) inflate the real balance above `PsmDebt`, but `redeem` still refuses to release more than `debt`, leaving the excess (`donation`) permanently parked in the reserve account: [2](#0-1) 

Reviewing the governance surface of the pallet (`set_minting_fee`, `set_redemption_fee`, `set_max_debt`, `set_asset_status`, `remove_external_asset`, `remove_psm`), none of these extrinsics allow reclaiming reserve balance in excess of `PsmDebt` — `remove_psm` even requires aggregate debt to be exactly zero before allowing removal, and never inspects or sweeps residual asset balances held in the reserve account: [3](#0-2) 

This is structurally identical to the `ConvexStakingWrapper`/`Shelter.sol` bug: `amountInShelter` (a tracked counter) is used to move funds around while the real balance can exceed it via `donate`, and there is no path to reconcile "actual balance minus tracked accounting" back to a rightful owner — the excess is simply lost forever once the counter-based exit path is taken.

### Impact Explanation
Any tokens that land in a PSM reserve account beyond the amount reflected in `PsmDebt` — whether from a user mistake, a third-party integrator sending funds directly to the well-known deterministic reserve address, or a deliberate top-up — become permanently locked. This is a "permanent user-fund lock" per the impact gate: value enters the system and can never be extracted by any account (user or admin), since redemption is gated on `PsmDebt` and there is no sweep/reclaim extrinsic. Because PSM reserve addresses are deterministically derived and public (`blake2_256((PalletId::TYPE_ID, PalletId, internal_asset).encode())`), this is trivially triggerable by any unprivileged party sending an external asset directly to that account rather than calling `mint`.

### Likelihood Explanation
High: no privileged action, governance, or special conditions are required. Any signed account holding the approved external asset can transfer it directly to the deterministic PSM reserve account (instead of using `mint`) and permanently lock those funds with a single transaction. The pallet's own test (`fails_when_reserve_exceeds_debt_donated_reserves`) demonstrates the exact state is reachable and that redemption capacity does not grow with real balance, only with `PsmDebt`.

### Recommendation
Add a mechanism to reconcile the two values, analogous to the report's suggested fix ("anything above `amountInShelter` is donated and should be recoverable"): expose an admin-only (or permissionless-to-a-fixed-beneficiary) extrinsic that computes `actual_reserve_balance - tracked_debt_for_asset` and transfers the excess to the PSM's `fee_destination`, the depositor, or another designated beneficiary, rather than leaving it permanently stranded. Alternatively, treat any balance increase beyond `PsmDebt` at `mint`/`redeem` time as auto-creditable debt so it becomes redeemable by the next caller instead of orphaned.

### Proof of Concept
The pallet's existing regression test demonstrates the locked-fund condition end-to-end: [2](#0-1) 

1. `debt = PsmDebt::<Test>::get(...)` — reserve is at parity with tracked debt.
2. `fund_external_asset(USDC_ASSET_ID, psm_account(), donation)` — simulates a direct/accidental transfer of `donation` tokens straight into the reserve account (bypassing `mint`), just like `Shelter::donate` transferring extra LP tokens into the shelter contract.
3. `reserve > debt` is asserted — the actual balance now exceeds the tracked accounting value.
4. Attempting `Psm::redeem(..., debt + 1, ...)` fails with `Error::<T>::InsufficientReserve` even though the account genuinely holds `debt + donation` tokens — the `donation` portion can never be redeemed by anyone, and no admin call exists to recover it.

### Citations

**File:** substrate/frame/psm/README.md (L53-63)
```markdown
### 2. Redeem (Internal → External)

```rust
redeem(origin, internal_asset, asset_id, amount)
```

- Burns `amount` of `internal_asset` from the user
- Transfers external asset from the instance's reserve to the user
- Redemption fee is transferred from the user as `internal_asset` to `fee_destination`
- Limited by the per-external tracked debt (`PsmDebt`), not raw reserve balance
- Requires `amount >= PsmInfo::min_swap_amount`
```

**File:** substrate/frame/psm/src/tests.rs (L664-718)
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
```

**File:** substrate/frame/psm/src/lib.rs (L1028-1053)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::remove_psm())]
		pub fn remove_psm(origin: OriginFor<T>, internal_asset: T::AssetId) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_remove_psm())?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;
			ensure!(info.external_count == 0, Error::<T>::PsmHasApprovedExternals);
			ensure!(Self::total_psm_debt(&internal_asset).is_zero(), Error::<T>::PsmHasDebt);

			let PsmAdminInfo { deposit, .. } =
				PsmAdmin::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;
			if let Some((depositor, ticket)) = deposit {
				ticket.drop(&depositor)?;
			}

			Psm::<T>::remove(&internal_asset);
			PsmAdmin::<T>::remove(&internal_asset);

			// Release the provider references acquired in `create_psm`. Reaps each account when
			// empty; a `ConsumerRemaining` error just means it still holds funds and must stay
			// alive, so the result is intentionally discarded.
			frame_system::Pallet::<T>::dec_providers(&Self::psm_account(&internal_asset)).ok();
			frame_system::Pallet::<T>::dec_providers(&info.fee_destination).ok();

			Self::deposit_event(Event::PsmRemoved { internal_asset });
			Ok(())
		}
```
