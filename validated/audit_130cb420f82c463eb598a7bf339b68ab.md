### Title
`Psm::mint`/`Psm::redeem` validate `min_swap_amount` against the gross amount, not the fee-adjusted net amount actually delivered - (File: substrate/frame/psm/src/lib.rs)

### Summary
`pallet-psm`'s `mint` and `redeem` extrinsics enforce a `BelowMinimumSwap` check against the **gross** (pre-fee) swap amount, but the amount actually delivered to the user is the **net** amount after the minting/redemption fee is deducted. There is no re-validation of the net amount against `info.min_swap_amount`, so a swap that "passes" the minimum-swap guarantee on-chain can still deliver an amount arbitrarily smaller than the advertised minimum once the fee is subtracted. This mirrors the reported Merkl `DistributionCreator._createCampaign` bug class exactly: minimum-threshold validation performed on the gross value, never re-checked on the fee-adjusted net value.

### Finding Description
In `mint()`: [1](#0-0) 

The check `ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap)` at line 722 is performed on `internal_equivalent`, the **gross** internal-asset value implied by the deposited external amount, before the minting fee is subtracted. The fee is then computed and subtracted to produce `internal_to_user`, which is the amount actually minted to the caller. `internal_to_user` is never compared against `info.min_swap_amount` again.

In `redeem()`: [2](#0-1) 

Likewise, `ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap)` at line 828 checks the caller-supplied `internal_amount` (gross, pre-fee), then `fee = fee_rate.mul_ceil(internal_amount)` and `internal_net = internal_amount.saturating_sub(fee)` compute the post-fee value that is actually burned/redeemed. The only subsequent check is `internal_net.is_zero() || !external_out.is_zero()` (`AmountTooSmallAfterConversion`) — this only prevents a literal zero output, not a below-threshold one.

The caller-supplied `max_fee: Permill` parameter (checked via `ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh)`) does not fix this: `max_fee` only bounds the *rate*, not the absolute *net output floor*. A user (or contract) satisfying `min_swap_amount` on the gross side with a legitimate, admin-configured fee rate that is non-trivial (e.g. tens of percent) will still receive net proceeds below `min_swap_amount`, with the pallet emitting a `Minted`/`Redeemed` event and no error — the operation "looks" compliant with the pallet's own minimum-swap guarantee while it isn't.

This is the direct analog of the Merkl bug: `min_swap_amount` is the PSM's equivalent of "minimum reward-per-hour", the minting/redemption fee is the equivalent of Merkl's protocol fee, and `internal_to_user` / `internal_net` are the equivalent of the net distributed amount that should have been, but wasn't, re-checked against the threshold.

### Impact Explanation
`min_swap_amount` is documented as a swap-size floor meant to prevent dust/uneconomical operations and (implicitly) to guarantee that swaps clear a meaningful backing/output threshold consistent with the PSM's 1:1 peg design. Because the check is bypassed for the actual net amount, users can mint or redeem amounts that undercut this guarantee — every fee-bearing swap right at (or moderately above) the minimum threshold silently delivers less than the protocol's stated floor. This degrades the intended peg-stability invariant ("swap amount below the instance's minimum threshold" is supposed to be rejected) and can be leveraged to force dust-level state changes (tiny `PsmDebt` deltas, tiny reserve transfers) that the minimum-swap guard was specifically designed to prevent, while still appearing fully compliant on-chain.

### Likelihood Explanation
This triggers under entirely normal, non-malicious conditions: any signed user calling `Psm::mint` or `Psm::redeem` with an amount just above `info.min_swap_amount` on any PSM instance where the configured `MintingFee`/`RedemptionFee` is non-negligible will hit this path automatically — no privileged actor, governance abuse, or adversarial setup is required (fee configuration by the PSM admin is a normal, in-scope pallet operation, not an attack). The bug is deterministic and reproducible on every such call.

### Recommendation
After computing the fee-adjusted output (`internal_to_user` in `mint`, `internal_net`/`external_out` in `redeem`), re-validate the net amount against `info.min_swap_amount` (in the appropriate unit) and reject with `Error::<T>::BelowMinimumSwap` if it falls short, instead of only checking the gross pre-fee amount.

### Proof of Concept
1. Admin creates a PSM with `min_swap_amount = 100 * INTERNAL_UNIT` via `create_psm`. [3](#0-2) 
2. Admin sets `MintingFee` for the external asset to e.g. `Permill::from_percent(50)` via `set_minting_fee`. [4](#0-3) 
3. User calls `Psm::mint(origin, internal_asset, external_asset, external_amount, max_fee=Permill::from_percent(50))` where `external_amount` converts to `internal_equivalent = 100 * INTERNAL_UNIT` (exactly at the minimum, passing the `BelowMinimumSwap` check at line 722).
4. `fee = 50% * 100 * INTERNAL_UNIT = 50 * INTERNAL_UNIT`; `internal_to_user = 50 * INTERNAL_UNIT` — half of the advertised minimum swap amount, minted to the user with a successful `Event::Minted`, no error raised despite `internal_to_user < info.min_swap_amount`. [5](#0-4) 
5. The existing test suite corroborates that fee deduction is applied and outputs are checked against balances but never against `min_swap_amount` post-fee (e.g. `fee_nonzero` test asserts `external_to_user = redeem_amount - fee` without any minimum-threshold assertion on the net amount). [6](#0-5)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L719-766)
```rust
			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;

			let fee_rate = MintingFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_equivalent);
			let internal_to_user = internal_equivalent.saturating_sub(fee);

			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);

			let psm_account = Self::psm_account(&internal_asset);
			T::Fungibles::transfer(
				external_asset.clone(),
				&who,
				&psm_account,
				effective_external,
				Preservation::Expendable,
			)?;
			T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
			if !fee.is_zero() {
				T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?;
			}

			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);

			Self::deposit_event(Event::Minted {
				who,
				internal_asset,
				external_asset,
				external_consumed: effective_external,
				internal_received: internal_to_user,
				internal_fee: fee,
			});
			Ok(())
```

**File:** substrate/frame/psm/src/lib.rs (L817-840)
```rust
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;

			ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_amount);
			let internal_net = internal_amount.saturating_sub(fee);

			let external_out =
				Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
			ensure!(
				internal_net.is_zero() || !external_out.is_zero(),
				Error::<T>::AmountTooSmallAfterConversion
			);
```

**File:** substrate/frame/psm/src/lib.rs (L940-953)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::create_psm())]
		pub fn create_psm(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			full_admin: Box<T::PalletsOrigin>,
			emergency_admin: Box<T::PalletsOrigin>,
			fee_destination: T::AccountId,
			max_debt: BalanceOf<T>,
			min_swap_amount: BalanceOf<T>,
		) -> DispatchResult {
			let maybe_depositor = T::CreateOrigin::ensure_origin(origin, &internal_asset)?;
			ensure!(!Psm::<T>::contains_key(&internal_asset), Error::<T>::PsmAlreadyExists);
			ensure!(!min_swap_amount.is_zero(), Error::<T>::ZeroMinSwapAmount);
```

**File:** substrate/frame/psm/src/lib.rs (L1075-1097)
```rust
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::set_minting_fee())]
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

**File:** substrate/frame/psm/src/tests.rs (L459-482)
```rust
	#[test]
	fn fee_nonzero() {
		ExtBuilder::default().mints(ALICE, 5000 * INTERNAL_UNIT).build_and_execute(|| {
			set_redemption_fee(USDC_ASSET_ID, Permill::from_percent(5));

			let redeem_amount = 1000 * INTERNAL_UNIT;
			let fee = Permill::from_percent(5).mul_ceil(redeem_amount);
			let external_to_user = redeem_amount - fee;
			let alice_usdc_before = get_asset_balance(USDC_ASSET_ID, ALICE);

			assert_ok!(Psm::redeem(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				redeem_amount,
				Permill::from_percent(5)
			));

			assert_eq!(
				get_asset_balance(USDC_ASSET_ID, ALICE),
				alice_usdc_before + external_to_user
			);
		});
	}
```
