### Title
Peg Stability Module (`pallet-psm`) mints/burns and moves funds before updating `PsmDebt`, violating Checks-Effects-Interactions and allowing debt-ceiling bypass on reentrant `Fungibles` implementations - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm::mint` and `pallet-psm::redeem` compute and validate debt-ceiling limits from `PsmDebt` storage, then perform the external-facing token operations (`T::Fungibles::transfer`, `T::Fungibles::mint_into`, `T::Fungibles::burn_from`), and only *afterwards* write the updated `PsmDebt` value. This is the exact pattern described in the NextGen report: the invariant-tracking state is updated **after** the operation that can trigger re-entrant execution, instead of before it (Checks-Effects-Interactions). [1](#0-0) [2](#0-1) 

### Finding Description
In `Pallet::<T>::mint` (`substrate/frame/psm/src/lib.rs`):
1. `current_total_psm_debt` and per-asset `current_debt`/`max_debt` are read from storage and checked against the requested `internal_equivalent` amount.
2. `T::Fungibles::transfer` moves the external asset from the caller into the PSM reserve account, then `T::Fungibles::mint_into` mints the internal asset to the caller (and to `fee_destination` if a fee applies).
3. Only after both of these interactions succeed does the pallet write `PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt)`. [3](#0-2) 

`redeem` has the mirrored ordering: `PsmDebt` and reserve checks are read first, then `T::Fungibles::transfer` (fee), `T::Fungibles::burn_from`, and `T::Fungibles::transfer` (external payout to the caller) are executed, and `PsmDebt::<T>::mutate` decrementing the debt happens last. [4](#0-3) 

`T::Fungibles` is a fully generic associated type on `pallet_psm::Config`, satisfied by whatever fungibles implementation the integrating runtime supplies. Nothing in `pallet-psm` constrains that implementation to be free of callbacks into the caller's own logic (e.g. an asset-transactor/precompile-backed fungibles implementation, a wrapped XCM-executor-driven asset, or any `fungibles::Mutate` implementation with `Freezer`/hook logic that invokes further runtime code on `mint_into`/`transfer`/`burn_from`). If any such implementation performs a synchronous callback during `mint_into`/`transfer` (as pallet-revive precompiles or custom hook-bearing token implementations can), the callback can re-enter `Psm::mint` (or `Psm::redeem`) before `PsmDebt` has been updated. Because the ceiling checks (`ExceedsMaxPsmDebt`, `InsufficientReserve`) are evaluated against the *stale* `PsmDebt`/reserve values on every re-entrant call, an attacker-controlled recipient can repeatedly re-enter and mint (or redeem) far beyond the configured `max_debt`/`min_swap_amount` bounds — exactly the "state updated after the external call" defect flagged as High severity in the NextGen finding.

This mirrors the root cause called out by the report: the guard (`maxCollectionPurchases` there, `max_debt`/`InsufficientReserve` here) is bypassed not because the check is missing, but because the bookkeeping mutation that keeps the check meaningful is placed after — not before — the state-mutating/external-facing call.

### Impact Explanation
If exploited on a runtime whose `T::Fungibles` implementation is not a pure, non-reentrant storage map (i.e., any implementation that triggers further logic during `mint_into`, `burn_from`, or `transfer`), an attacker can:
- Mint internal stablecoin far beyond the PSM's configured `max_debt` ceiling, i.e. unbacked mint (theft/inflation of the internal asset without proportional external collateral in the reserve).
- Or drain more external reserve on redemption than `PsmDebt`/actual backing allows, causing permanent loss of reserve funds for the remaining legitimate holders of the internal asset.
Both outcomes break the core peg invariant ("PSM Debt... backed 1:1 by external assets in that PSM's reserve") documented in the pallet's own module docs, and directly match the "theft or unbacked mint" / "duplicate settlement" impact categories in scope.

### Likelihood Explanation
Likelihood depends entirely on whether a given runtime's `T::Fungibles` binding is reentrant. The pallet itself provides no defense (no `TransactionalMode`/reentrancy guard is used around the mint/redeem interaction block), so any current or future runtime configuration that wires a hook-bearing or precompile-backed fungibles implementation into `pallet-psm::Config::Fungibles` inherits this vulnerability unconditionally and without any additional attacker capability (no privileged role, no relayer/validator assumption — a normal signed account can trigger it). The unprivileged, public-entrypoint nature of `mint`/`redeem` combined with the unconditional ordering bug makes this a structurally live issue rather than a theoretical one, even though it is currently latent for whichever concrete `Fungibles` type the shipped runtimes use.

### Recommendation
Apply the Checks-Effects-Interactions pattern as the original report recommends:
- In `mint`, write `PsmDebt::<T>::insert(...)` (and any other ceiling-relevant bookkeeping) immediately after the ensure!/ceiling checks and **before** calling `T::Fungibles::transfer`/`mint_into`.
- In `redeem`, apply `PsmDebt::<T>::mutate(...)` decrement **before** calling `T::Fungibles::transfer`/`burn_from`.
- Additionally, wrap the mint/redeem body in a reentrancy guard (e.g., a per-account or per-instance `StorageValue<bool>` flag checked/set at entry and cleared at exit) so that even if a future `Fungibles` implementation is reentrant, nested calls into `Psm::mint`/`Psm::redeem` are rejected outright, independent of ordering.

### Proof of Concept
Conceptual PoC (mirrors the NextGen PoC structure):
1. Configure (or imagine a future configuration of) `pallet_psm::Config::Fungibles` with an implementation whose `mint_into` invokes a hook/callback into the recipient's account logic (e.g., a precompile-backed asset or an asset with a `Freezer`/`OnNewAccount` style callback that can execute attacker logic).
2. Attacker account calls `Psm::mint(internal_asset, external_asset, external_amount, max_fee)` with enough external asset to sit just under `max_debt`.
3. During the `T::Fungibles::mint_into(internal_asset, &who, internal_to_user)` call inside step 2, the hook synchronously re-enters `Psm::mint` with the same parameters.
4. Because `PsmDebt` has not yet been updated by the first (outer) call, the `ExceedsMaxPsmDebt` check in the re-entrant call still sees the pre-mint debt value and succeeds, minting again.
5. Repeat via nested re-entry until unwinding; each nested call successfully mints internal asset while only the outermost call's `PsmDebt::insert` is ultimately applied — internal asset supply grows past `max_debt` while `PsmDebt` bookkeeping reflects only a single mint, permanently under-recording real minted debt relative to reserve backing.

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

**File:** substrate/frame/psm/src/lib.rs (L843-891)
```rust
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
```
