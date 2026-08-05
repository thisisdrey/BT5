Audit Report

## Title
Donated / Accidentally-Transferred Reserve Tokens Are Permanently Locked In `pallet-psm` Reserve Accounts - (File: `substrate/frame/psm/src/lib.rs`)

## Summary
`pallet-psm`'s reserve account for each PSM instance holds external-asset tokens, but the amount redeemable via `redeem` is capped by the separately tracked `PsmDebt` storage item rather than the reserve account's actual token balance. Any external-asset tokens that reach the reserve account beyond what `PsmDebt` records — via a direct/accidental transfer or an intentional "donation" — become structurally unrecoverable, as no extrinsic reconciles or sweeps the excess.

## Finding Description
As documented, redemption is explicitly bounded by the tracked debt counter, not the raw reserve balance: [1](#0-0) . The reserve account is a deterministic, publicly derivable address (`blake2_256((PalletId::TYPE_ID, PalletId, internal_asset).encode())`) as noted in the pallet docs [2](#0-1) , meaning any external party can transfer tokens directly to it, bypassing `mint`, and inflate the real balance above `PsmDebt`.

Reviewing the governance surface, `remove_psm` requires the aggregate debt to be exactly zero but never inspects or sweeps any residual reserve balance held in the account before removing the PSM instance and releasing provider references: [3](#0-2) . No other governance extrinsic (`set_minting_fee`, `set_redemption_fee`, `set_max_debt`, `set_asset_status`, `remove_external_asset`) provides a sweep/reclaim path for reserve balance in excess of `PsmDebt`.

The pallet's own regression test confirms this is reachable: funds sent directly to `psm_account()` inflate the real reserve balance above `PsmDebt`, and subsequent `redeem` calls attempting to withdraw more than `debt` fail with `InsufficientReserve`, even though the account genuinely holds `debt + donation` tokens: [4](#0-3) .

## Impact Explanation
This matches the "permanent user-fund lock" impact: value (external asset tokens) enters the reserve account and can never be extracted by any account — user or admin — because redemption capacity is gated strictly by `PsmDebt`, and no sweep/reclaim mechanism exists. Since PSM reserve addresses are deterministic and derivable from public inputs, this is not a contrived or privileged scenario; it is an ordinary account-holder action (a transfer) that produces an unrecoverable state affecting the exact corrupted/stranded value: the delta between the reserve account's real external-asset balance and `PsmDebt`.

## Likelihood Explanation
High. No privileged action, governance, or special conditions are required — a single unprivileged signed account can call the standard asset-transfer extrinsic to send external-asset tokens directly to the deterministic PSM reserve account instead of calling `mint`, permanently locking those funds in one transaction. The pallet's own test (`fails_when_reserve_exceeds_debt_donated_reserves`) demonstrates this exact state is reachable via ordinary token transfers and that redemption capacity never grows with the real balance, only with `PsmDebt`.

## Recommendation
Add a mechanism to reconcile `actual_reserve_balance - tracked_debt_for_asset`: expose an admin-only or permissionless-to-fixed-beneficiary extrinsic that sweeps the excess to the PSM's `fee_destination` or another designated beneficiary. Alternatively, treat any balance increase beyond `PsmDebt` as auto-creditable debt at `mint`/`redeem` time so it becomes redeemable rather than stranded, and have `remove_psm` check/handle residual reserve balances before removal.

## Proof of Concept
1. Query `debt = PsmDebt::<Test>::get(internal_asset, external_asset)` for an active PSM — reserve balance is at parity with tracked debt.
2. Any account calls the standard asset-transfer extrinsic to send `donation` amount of the external asset directly to `psm_account(internal_asset)`, bypassing `Psm::mint`.
3. Assert `get_asset_balance(external_asset, psm_account()) > debt` — real balance now exceeds tracked accounting.
4. Fund a user with enough internal asset and call `Psm::redeem(..., debt + 1, ...)` — this fails with `Error::<T>::InsufficientReserve` despite the account genuinely holding `debt + donation` tokens.
5. No extrinsic in the pallet (mint, redeem, or any admin call including `remove_psm`) provides a path to recover the `donation` portion; it is permanently stranded, as demonstrated by `fails_when_reserve_exceeds_debt_donated_reserves` in `substrate/frame/psm/src/tests.rs`.

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

**File:** substrate/frame/psm/src/lib.rs (L54-59)
```rust
//! * **PSM instance**: A configured Peg Stability Module, keyed by its internal asset id and
//!   described by [`PsmInfo`]. Each instance has its own reserve account derived from
//!   `blake2_256((PalletId::TYPE_ID, PalletId, internal_asset).encode())`.
//! * **Minting**: Deposit external asset → receive internal asset (minus fee).
//! * **Redemption**: Burn internal asset → receive external asset (minus fee).
//! * **Reserve**: External asset balance held by a PSM's reserve account (derived, not stored).
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
