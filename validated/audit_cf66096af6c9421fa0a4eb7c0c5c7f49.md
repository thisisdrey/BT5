Audit Report

## Title
Donated / Accidentally-Transferred Reserve Tokens Are Permanently Locked In `pallet-psm` Reserve Accounts - (File: `substrate/frame/psm/src/lib.rs`)

## Summary
`pallet-psm` tracks per-instance redeemable capacity via the `PsmDebt` storage item rather than the actual token balance held by the deterministic reserve account (`Pallet::psm_account`). Any external-asset tokens that land in a reserve account beyond the amount reflected in `PsmDebt` — via a direct transfer, integrator mistake, or intentional donation — become permanently unrecoverable because `redeem` is hard-capped by `PsmDebt` and no extrinsic exists to sweep or reclaim the excess.

## Finding Description
In `redeem`, the maximum internal amount that can be converted back to external asset is gated strictly by the per-`(internal_asset, external_asset)` `PsmDebt` entry: [1](#0-0) 
This check (`current_debt >= effective_internal_net`) is independent of the real balance held by `Self::psm_account(&internal_asset)`, which is computed via `get_reserve`: [2](#0-1) 
The reserve account address is fully deterministic and derivable off-chain by any party from `blake2_256((PalletId::TYPE_ID, PalletId, internal_asset).encode())`: [3](#0-2) 
Because this address is a normal `AccountId`, any unprivileged account can transfer the approved external asset to it directly (bypassing `mint`), inflating the actual balance above `PsmDebt` without changing `PsmDebt`. `redeem`'s cap on `effective_internal_net` never grows to reflect this extra balance, so the excess can never be withdrawn by any account.

Reviewing every other dispatchable in the pallet (`create_psm`, `remove_psm`, `set_minting_fee`, `set_redemption_fee`, `set_max_debt`, `set_asset_status`, `set_asset_ceiling_weight`, `add_external_asset`, `remove_external_asset`, `set_full_admin`, `set_emergency_admin`), none of them read or reconcile the reserve account's actual balance against `PsmDebt`, and none provide a sweep/reclaim path. `remove_psm` even requires `total_psm_debt` to be exactly zero and only releases provider references — it never inspects or moves residual asset balances: [4](#0-3) 
The pallet's own `do_try_state` invariant explicitly only asserts `reserve >= debt_as_external` (i.e., surplus is tolerated, not flagged or fixed): [5](#0-4) 

## Impact Explanation
This matches the Impact Gate's "permanent user-fund or bridge-state lock" category: value that enters a `pallet-psm` reserve account beyond `PsmDebt` for that `(internal_asset, external_asset)` pair can never be extracted by any account — user or admin — because `redeem` is the only extrinsic that moves funds out of the reserve, and it is capped by the corrupted/insufficient tracking value `PsmDebt`, not the actual balance. The exact stranded value is `get_reserve(internal_asset, external_asset) - PsmDebt::<T>::get(internal_asset, external_asset)`.

## Likelihood Explanation
High and trivially reachable: no privileged action or governance is required. Any signed account holding the approved external asset can compute the deterministic reserve address and issue a normal `transfer` (via the underlying `Fungibles` implementation, e.g. `pallet-assets`) directly to it instead of calling `mint`, locking those funds permanently in a single transaction. The pallet's regression test `fails_when_reserve_exceeds_debt_donated_reserves` in `substrate/frame/psm/src/tests.rs` demonstrates this exact state is reachable and that `redeem` capacity is bound to `PsmDebt`, not the real balance.

## Recommendation
Add a mechanism to reconcile the two values — e.g., an admin-only (or permissionless-to-fixed-beneficiary) extrinsic that computes `get_reserve(internal_asset, external_asset) - PsmDebt::<T>::get(internal_asset, external_asset)` and transfers the excess to `PsmInfo::fee_destination` or another designated beneficiary. Alternatively, treat balance increases beyond `PsmDebt` observed during `mint`/`redeem` as auto-creditable debt so the surplus becomes redeemable by subsequent callers instead of being permanently orphaned.

## Proof of Concept
1. Create a PSM instance for `INTERNAL_ASSET_ID` with `USDC_ASSET_ID` approved, establishing `psm_account = Pallet::psm_account(&INTERNAL_ASSET_ID)`.
2. Have an unprivileged account transfer `donation` units of `USDC_ASSET_ID` directly to `psm_account` (bypassing `mint`), e.g. via `Assets::transfer`.
3. Observe `get_reserve(INTERNAL_ASSET_ID, USDC_ASSET_ID) = PsmDebt::<T>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID) + donation`.
4. Any account attempts `Psm::redeem(..., PsmDebt + 1, ...)` and it fails with `Error::<T>::InsufficientReserve`, confirmed by the existing test `fails_when_reserve_exceeds_debt_donated_reserves` in `substrate/frame/psm/src/tests.rs` (lines 664-718), even though the reserve genuinely holds `debt + donation` tokens.
5. No extrinsic in the pallet (including `remove_psm`, which requires debt to already be zero) provides any path to move the `donation` amount out of `psm_account`, confirming permanent lock.

### Citations

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

**File:** substrate/frame/psm/src/lib.rs (L1030-1053)
```rust
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

**File:** substrate/frame/psm/src/lib.rs (L1500-1507)
```rust
		/// Derive the reserve account for a PSM instance from the full hash of the pallet-id
		/// domain separator, pallet id, and internal asset.
		pub fn psm_account(internal_asset: &T::AssetId) -> T::AccountId {
			let entropy = (<PalletId as TypeId>::TYPE_ID, T::PalletId::get(), internal_asset)
				.using_encoded(sp_io::hashing::blake2_256);
			T::AccountId::decode(&mut TrailingZeroInput::new(entropy.as_ref()))
				.expect("All byte sequences are valid `AccountId`s; qed")
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1567-1573)
```rust
		/// Balance of an external held by a PSM instance's reserve account.
		pub(crate) fn get_reserve(
			internal_asset: &T::AssetId,
			external_asset: &T::AssetId,
		) -> BalanceOf<T> {
			T::Fungibles::balance(external_asset.clone(), &Self::psm_account(internal_asset))
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1693-1702)
```rust
					// 1. Per-external reserve covers tracked debt.
					let debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
					let reserve = Self::get_reserve(&internal_asset, &external_asset);
					let debt_as_external =
						Self::internal_to_external(debt, external.decimals, info.internal_decimals)
							.map_err(|_| "Failed to convert tracked debt to external units")?;
					ensure!(
						reserve >= debt_as_external,
						"PSM reserve is less than tracked debt for an asset"
					);
```
