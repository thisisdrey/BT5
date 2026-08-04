Now I have enough evidence to identify a genuine local analog of the BlueBerry accounting bug.

### Title
`pallet-psm::mint` uses `T::Fungibles::transfer`'s nominal amount instead of the actual value moved, letting fee-on-transfer/rebasing external assets create unbacked internal-asset debt - ([File: substrate/frame/psm/src/lib.rs])

### Summary
The BlueBerry bug is: an external call that can silently take a fee returns/implies less value than what the internal ledger records, so the ledger keeps "phantom" backing that can be leveraged. `pallet-psm::mint` has the same shape: it calls `T::Fungibles::transfer(external_asset, &who, &psm_account, effective_external, Preservation::Expendable)` and then unconditionally credits `internal_to_external`-derived `internal_equivalent` to `PsmDebt` and mints `internal_to_user` to the user, using the *requested* `effective_external` amount rather than any actually-received amount at the reserve account.

### Finding Description
In `mint` (`substrate/frame/psm/src/lib.rs:702-767`): [1](#0-0) 
the flow is:
1. Compute `internal_equivalent` from the *requested* `external_amount`.
2. Call `T::Fungibles::transfer(external_asset, &who, &psm_account, effective_external, ...)`.
3. Mint `internal_to_user` and fee to the user/fee destination.
4. Record `new_debt = current_debt + internal_equivalent` into `PsmDebt`.

`PsmDebt` and the minted internal amount are computed purely from the *nominal* `effective_external`/`internal_equivalent` values, not from what the PSM reserve account actually received. `T::Fungibles::transfer` is generic over `Config::Fungibles`, which in principle can be configured to point at asset implementations that apply a transfer fee, deflationary/rebasing burn, or any other mechanism that makes the amount credited to `psm_account` smaller than the amount debited from `who` (this is exactly the class of asset the BlueBerry report is about — one where `transfer`/`withdraw` do not guarantee 1:1 delivery). Since `mint` never re-reads the reserve's actual post-transfer balance and never checks the delta, the PSM debt (`PsmDebt`) and the newly minted internal asset are increased by the full nominal `internal_equivalent`, even if the reserve backing that debt is short by the fee amount.

This mirrors the exact BlueBerry pattern: `wAmount = ISoftVault(bank.softVault).withdraw(shareAmount);` is trusted as the real transferred amount and used to update `pos.underlyingAmount`, when a withdraw fee means less was actually moved. Here, the "trusted nominal amount" is `effective_external`/`internal_equivalent`, used to update `PsmDebt` and mint internal tokens, without validating that the reserve account's balance increased by that amount.

By contrast, the `redeem` path in the same pallet is explicitly hardened against exactly this class of bug — its comments (`substrate/frame/psm/src/lib.rs:841-844`) show the author deliberately tracking `effective_internal_net` (the round-tripped value) rather than the raw requested amount, to avoid dust/rounding based over- or under-accounting: [2](#0-1) 
This asymmetry — `redeem` carefully aligns burned/debt-reduced amounts with what was actually transacted, while `mint` does not verify what was actually received into the reserve before minting and updating debt — is the root of the issue.

### Impact Explanation
If `T::Fungibles` is configured (directly or via a wrapping adapter) with any external asset whose `transfer` implementation does not guarantee full nominal delivery (fee-on-transfer token, deflationary/rebase token, or any fungible implementation that legitimately returns success while moving less than the requested amount), a user can:
1. Call `mint` with such an asset.
2. Receive full-value `internal_to_user` minted internal stablecoin.
3. Have `PsmDebt` incremented by the full nominal `internal_equivalent`.
4. Leave the PSM reserve under-collateralized by the fee amount, while the debt ledger claims full backing.

Because `PsmDebt` is used to gate future redemptions (`current_debt >= effective_internal_net` in `redeem`) and to enforce the debt ceiling (`ExceedsMaxPsmDebt`), the shortfall is invisible until enough users redeem and the reserve cannot cover `external_out` for the last redeemers (`Error::InsufficientReserve`/`defensive!("PSM reserve is less than expected output amount")` at `substrate/frame/psm/src/lib.rs:851-855`), at which point some legitimate holders are permanently unable to redeem — a fund-lock/insolvency condition for the internal stablecoin's peg, i.e. unbacked mint.

### Likelihood Explanation
Exploitability depends entirely on whether a given runtime's `Config::Fungibles` allows registering an `external_asset` whose transfer semantics do not deliver the nominal amount (e.g., a permissionless-listing configuration, or a bridged/wrapped asset with built-in fees). This is a config-dependent risk rather than a universally-exploitable bug in the pallet's core logic on every deployment, so likelihood is moderate and gated by `add_external_asset` governance/admin decisions about which assets are approved — but the pallet itself provides no defensive check (e.g., comparing pre/post reserve balance) regardless of asset choice, so the protection currently relies entirely on external asset selection rather than pallet-level invariants.

### Recommendation
In `mint`, capture the PSM reserve account's balance of `external_asset` before and after the `T::Fungibles::transfer` call, and use the actual delta (not the nominal `effective_external`) to derive the internal amount minted and the debt recorded in `PsmDebt`. This mirrors the defensive pattern already used in `redeem`, and would ensure `PsmDebt` never overstates the backing actually held in reserve, regardless of the transfer semantics of a given `external_asset`.

### Proof of Concept
Not independently executable from static review alone — a concrete PoC would require configuring a runtime's `Config::Fungibles`/`add_external_asset` with an external asset implementation that has fee-on-transfer or rebase semantics (i.e., where `Fungibles::transfer(asset, from, to, amount, ..)` succeeds but the balance of `to` increases by less than `amount`), then calling `Psm::mint` and observing that `PsmDebt` and `T::Fungibles::balance(internal_asset, who)` increase by the full nominal amount while `T::Fungibles::balance(external_asset, psm_account)` increases by less, producing an under-collateralized `PsmDebt` that later triggers `Error::InsufficientReserve` on `redeem` for other users. [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L702-767)
```rust
		pub fn mint(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			external_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

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
		}
```

**File:** substrate/frame/psm/src/lib.rs (L809-902)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::redeem())]
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
			// `effective_internal_net` is the internal value that round-trips to `external_out`;
			// it is what we actually burn and what the tracked debt decreases by. Any truncation
			// dust stays in the caller's internal balance, symmetric with `mint`, which takes
			// only the round-tripped share of the external amount.
			let effective_internal_net =
				Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}

			if !fee.is_zero() {
				T::Fungibles::transfer(
					internal_asset.clone(),
					&who,
					&info.fee_destination,
					fee,
					Preservation::Expendable,
				)?;
			}

			if !effective_internal_net.is_zero() {
				T::Fungibles::burn_from(
					internal_asset.clone(),
					&who,
					effective_internal_net,
					Preservation::Expendable,
					Precision::Exact,
					Fortitude::Polite,
				)?;
			}

			let psm_account = Self::psm_account(&internal_asset);
			if !external_out.is_zero() {
				T::Fungibles::transfer(
					external_asset.clone(),
					&psm_account,
					&who,
					external_out,
					Preservation::Expendable,
				)?;
			}

			PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
				*debt = debt.saturating_sub(effective_internal_net);
			});

			Self::deposit_event(Event::Redeemed {
				who,
				internal_asset,
				external_asset,
				internal_consumed: effective_internal_net.saturating_add(fee),
				external_received: external_out,
				internal_fee: fee,
			});
			Ok(())
		}
```
