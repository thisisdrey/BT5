Audit Report

## Title
Unprotected AMM refund swap in `SwapFirstAssetTrader::refund_weight` allows sandwich extraction of XCM fee-refund value - (File: cumulus/primitives/utility/src/lib.rs)

## Summary
`SwapFirstAssetTrader::refund_weight` swaps leftover `Target`-denominated fee credit back into the user's original fee asset by calling `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None`, unlike `buy_weight`, which protects the paying leg via a fixed-output `swap_tokens_for_exact_tokens` call. This trader is wired into live XCM `Trader` configs for Asset Hub Rococo, Asset Hub Westend, Penpal, and the staking-async parachain runtime, all backed by `pallet-asset-conversion` pools that any unprivileged account can manipulate via ordinary `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` extrinsics.

## Finding Description
`refund_weight` in `cumulus/primitives/utility/src/lib.rs` extracts the unused portion of `total_fee` (already-collected `Target`-asset credit) and swaps it back to the original fee asset: [1](#0-0) 
passing `None` for `amount_out_min` unconditionally. This is confirmed against the actual repository code, matching the claim exactly.

By contrast, `buy_weight` protects the user-paying leg by requesting a fixed output amount via `swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)`, so only the refund path lacks any output floor: [2](#0-1) 

The `SwapCredit` trait explicitly supports optional slippage protection through `amount_out_min: Option<Self::Balance>`, and the pallet-level implementation (`impl<T: Config> SwapCredit<T::AccountId> for Pallet<T>`) forwards this to `do_swap_exact_credit_tokens_for_tokens`, which enforces the minimum only when `Some` is supplied: [3](#0-2) 
Passing `None` therefore fully bypasses this protection at the AMM level, and every other public swap-related extrinsic (`swap_exact_tokens_for_tokens`, `remove_liquidity`) enforces a caller-supplied minimum: [4](#0-3) [5](#0-4) 

`SwapFirstAssetTrader` is confirmed wired into `type Trader` in Asset Hub Rococo's `xcm_config.rs`, Asset Hub Westend's `xcm_config.rs`, Penpal's `xcm_config.rs`, and the staking-async parachain runtime's `xcm_config.rs`, over `crate::AssetConversion` pools: [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

These are ordinary constant-product `pallet-asset-conversion` pools that any unprivileged account can shift via public `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` extrinsics immediately before an XCM message that triggers `refund_weight`, then reverse afterward, sandwich-extracting value from the unprotected refund swap.

## Impact Explanation
The corrupted value is the refunded amount of the user's original fee asset (`refund_swap_asset`), derived from `self.total_fee` (already-collected fee credit held by the trader/protocol). Because the refund leg accepts any nonzero output, a manipulated pool state at refund time can arbitrarily reduce the refunded amount, with the deficit captured by whoever manipulated the pool. This is a real, in-repo economic bug pattern — public underpriced/unprotected AMM interaction causing loss of user/protocol-held funds — but it is bounded by the size of `total_fee` refunds and pool liquidity depth; it does not compromise chain consensus, forge proofs, or escalate origin. It fits the "theft/loss of protocol or user funds via unprotected AMM interaction" impact class rather than a chain-halting or bridge-critical severity tier.

## Likelihood Explanation
No privileged role, governance action, or compromised node/validator is required. An unprivileged attacker only needs ordinary `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` calls against a `Target`/fee-asset pool exercised by `SwapFirstAssetTrader`, timed with legitimate XCM traffic that triggers a `refund_weight` call after paying fees in a non-native asset. The precondition — shallow-liquidity pools between `Target` and a foreign/trust-backed fee asset — is realistic on Asset Hub/Penpal, since pools can be created by any user and new/thin pools are common.

## Recommendation
Do not pass `None` as `amount_out_min` in `refund_weight`. Compute an acceptable minimum at refund time — e.g., via the already-available `QuotePrice::quote_price_exact_tokens_for_tokens(Target::get(), refund_swap_asset, refund_amount, true)` minus a configurable tolerance — and pass `Some(min_out)` to `SwapCredit::swap_exact_tokens_for_tokens`. On swap failure (slippage exceeded), fall back to holding the `Target`-denominated credit in `total_fee` (as the existing error branch already does) or returning it via an alternative safe path, rather than accepting an unbounded-slippage swap.

## Proof of Concept
1. Deploy a parachain runtime mirroring Asset Hub's `xcm_config.rs`, configuring `type Trader` to include `SwapFirstAssetTrader<Target, AssetConversion, ...>` over a `pallet-asset-conversion` pool for `Target`/`AssetX` with shallow liquidity [10](#0-9) .
2. Attacker submits `AssetConversion::swap_exact_tokens_for_tokens` (an unprivileged, public extrinsic) to skew the `Target`/`AssetX` reserve ratio unfavorably for `Target -> AssetX` conversions.
3. In the same block, a legitimate XCM message pays fees in `AssetX` via `buy_weight` (locking a fair `AssetX -> Target` rate for the `fee` amount) but under-consumes weight, triggering `refund_weight`, which swaps leftover `Target` back to `AssetX` at the skewed price with no floor [11](#0-10) .
4. Attacker reverses their swap, capturing the price impact as profit; the refunded `AssetX` amount received by the honest user is measurably below the pre-manipulation quoted price, with no error and no revert on this path. A Rust integration test extending `cumulus/primitives/utility/src/tests/swap_first.rs` can mock the pool state before/after the refund call and assert the refund amount is degraded relative to `QuotePrice::quote_price_exact_tokens_for_tokens` taken before the attacker's swap.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L471-475)
```rust
		let (credit_out, credit_change) = match SwapCredit::swap_tokens_for_exact_tokens(
			vec![swap_asset, Target::get()],
			credit_in,
			fee,
		) {
```

**File:** cumulus/primitives/utility/src/lib.rs (L539-561)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
			Ok(refund_in_target) => refund_in_target,
			Err((refund, _)) => {
				// return an attempted refund back to the `total_fee`.
				let _ = self.total_fee.subsume(refund).map_err(|refund| {
					// error may occur if `total_fee.asset` differs from `refund.asset`, which does
					// not apply in this context.
					defensive!(
						"`total_fee.asset` must be equal to `refund.asset`",
						(self.total_fee.asset(), refund.asset())
					);
				});
				return None;
			},
		};

		let refund = AssetsInHolding::new_from_fungible_credit(refund_asset.id, Box::new(refund));
		Some(refund)
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L203-220)
```rust
	fn swap_exact_tokens_for_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out_min: Option<Self::Balance>,
	) -> Result<Self::Credit, (Self::Credit, DispatchError)> {
		let credit_asset = credit_in.asset();
		with_transaction(|| -> TransactionOutcome<Result<_, DispatchError>> {
			let res = Self::do_swap_exact_credit_tokens_for_tokens(path, credit_in, amount_out_min);
			match &res {
				Ok(_) => TransactionOutcome::Commit(Ok(res)),
				// wrapping `res` with `Ok`, since our `Err` doesn't satisfy the
				// `From<DispatchError>` bound of the `with_transaction` function.
				Err(_) => TransactionOutcome::Rollback(Ok(res)),
			}
		})
		// should never map an error since `with_transaction` above never returns it.
		.map_err(|_| (Self::Credit::zero(credit_asset), DispatchError::Corruption))?
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L922-929)
```rust
			ensure!(
				!amount1.is_zero() && amount1 >= amount1_min_receive,
				Error::<T>::AssetOneWithdrawalDidNotMeetMinimum
			);
			ensure!(
				!amount2.is_zero() && amount2 >= amount2_min_receive,
				Error::<T>::AssetTwoWithdrawalDidNotMeetMinimum
			);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L988-1002)
```rust
			ensure!(amount_in > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_out_min) = amount_out_min {
				ensure!(amount_out_min > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_in(amount_in, path)?;

			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/xcm_config.rs (L370-394)
```rust
	type Trader = (
		UsingComponents<
			WeightToFee,
			TokenLocation,
			AccountId,
			Balances,
			ResolveTo<StakingPot, Balances>,
		>,
		cumulus_primitives_utility::SwapFirstAssetTrader<
			TokenLocation,
			crate::AssetConversion,
			WeightToFee,
			crate::NativeAndNonPoolAssets,
			(
				TrustBackedAssetsAsLocation<
					TrustBackedAssetsPalletLocation,
					Balance,
					xcm::v5::Location,
				>,
				ForeignAssetsConvertedConcreteId,
			),
			ResolveAssetTo<StakingPot, crate::NativeAndNonPoolAssets>,
			AccountId,
		>,
	);
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L446-470)
```rust
	type Trader = (
		UsingComponents<
			WeightToFee,
			WestendLocation,
			AccountId,
			Balances,
			ResolveTo<StakingPot, Balances>,
		>,
		cumulus_primitives_utility::SwapFirstAssetTrader<
			WestendLocation,
			crate::AssetConversion,
			WeightToFee,
			crate::NativeAndNonPoolAssets,
			(
				TrustBackedAssetsAsLocation<
					TrustBackedAssetsPalletLocation,
					Balance,
					xcm::v5::Location,
				>,
				ForeignAssetsConvertedConcreteId,
			),
			ResolveAssetTo<StakingPot, crate::NativeAndNonPoolAssets>,
			AccountId,
		>,
	);
```

**File:** cumulus/parachains/runtimes/testing/penpal/src/xcm_config.rs (L399-413)
```rust
	type Trader = (
		// Allow native asset to pay the execution fee
		UsingComponents<WeightToFee, PenpalNativeCurrency, AccountId, Balances, ToAuthor<Runtime>>,
		// This trader allows to pay with any assets exchangeable to native asset with
		// [`AssetConversion`].
		cumulus_primitives_utility::SwapFirstAssetTrader<
			PenpalNativeCurrency,
			crate::AssetConversion,
			WeightToFee,
			crate::NativeAndAssets,
			(LocalAssetsConvertedConcreteId, ForeignAssetsConvertedConcreteId),
			ResolveAssetTo<StakingPot, crate::NativeAndAssets>,
			AccountId,
		>,
	);
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs (L404-427)
```rust
	type Trader = (
		UsingComponents<
			WeightToFee,
			WestendLocation,
			AccountId,
			Balances,
			ResolveTo<StakingPot, Balances>,
		>,
		cumulus_primitives_utility::SwapFirstAssetTrader<
			WestendLocation,
			crate::AssetConversion,
			WeightToFee,
			crate::NativeAndNonPoolAssets,
			(
				TrustBackedAssetsAsLocation<
					TrustBackedAssetsPalletLocation,
					Balance,
					xcm::v5::Location,
				>,
				ForeignAssetsConvertedConcreteId,
			),
			ResolveAssetTo<StakingPot, crate::NativeAndNonPoolAssets>,
			AccountId,
		>,
```
