### Title
`SwapFirstAssetTrader::buy_weight` prices XCM fee purchases off an instantaneously-manipulable `pallet-asset-conversion` pool, letting an attacker underpay execution fees via same-transaction swap-then-reverse - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
The reported Yield bug is a single-invariant class: a value that gates redemption/settlement (`exchangeRateStored`) is derived from mutable state (`totalBorrows`) that an unprivileged actor can push up and back down atomically (flash borrow → act → repay) in one transaction, so the settlement uses a value that never reflected real market conditions. The local analog is `SwapFirstAssetTrader::buy_weight`/`quote_weight` in `cumulus/primitives/utility/src/lib.rs`, which prices XCM execution fees using `SwapCredit`/`QuotePrice` calls into `pallet-asset-conversion`'s live pool reserves (`Pools::get_reserves`), with no time-weighting or manipulation resistance, and the pallet itself documents that reserves can be shifted atomically by chaining swap + liquidity calls in one transaction.

### Finding Description
`SwapFirstAssetTrader::buy_weight` ( [1](#0-0) ) swaps the user's fee-payment asset for the `Target` asset via `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)`. That call resolves to `pallet_asset_conversion`'s `get_amount_in`, which is computed directly from the pool's current reserves fetched from live balances, e.g. `get_reserves`/`get_balance` in `substrate/frame/asset-conversion/src/lib.rs` ( [2](#0-1) ) and `do_add_liquidity`'s reserve reads ( [3](#0-2) ). `QuotePrice` explicitly documents that its quote is only accurate "if no other swaps are made ... before the target swap" ( [4](#0-3) ), i.e. it is a spot price with no TWAP or manipulation guard.

The pallet's own `add_liquidity` documentation confirms atomic reserve manipulation is a supported, expected pattern: "batch an atomic call with `add_liquidity` and `swap_exact_tokens_for_tokens` ... to render the liquidity withdrawable and rectify the exchange rate" ( [5](#0-4) ). This proves reserves can be pushed to an arbitrary temporary ratio and restored within one atomic transaction (e.g. via `pallet-utility::batch_all` or multiple instructions inside one `pallet_xcm::execute` call), exactly mirroring the "flash borrow → act → repay" primitive from the Compound report: only the AMM's ~0.3% fee is paid, no capital is permanently committed, and the manipulated value is consumed by a settlement path (`buy_weight`) before it reverts.

An attacker who is the same actor initiating the XCM/fee-buying action can, in one transaction:
1. Swap a large amount of `Target` into the pool for `swap_asset` (or vice versa), skewing `reserve(swap_asset)`/`reserve(Target)` in the direction that minimizes `get_amount_in` for the coming `swap_tokens_for_exact_tokens(fee)` call.
2. Trigger `SwapFirstAssetTrader::buy_weight` (via an XCM message they control, e.g. `pallet_xcm::execute` with `BuyExecution`), which computes `amount_in` against the now-skewed reserves and takes an artificially small amount of `swap_asset` from the attacker's own `credit_in` for `fee` amount of `Target`.
3. Reverse the initial swap to restore reserves and recover most of the previously swapped-out asset.

The net effect: the attacker pays LP-subsidized/underpriced fees for weight execution, and the loss is absorbed by the pool's other liquidity providers (their reserves are used to fill the exact-output swap at a rate the attacker manufactured), i.e. value is extracted from the pool without conservation, and public/XCM execution work is paid for below its true market cost.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" and "theft ... value from ... contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount": the fee-buying mechanism settles at a price the payer themselves manufactured in the same atomic operation, extracting value from LPs in the pool and letting XCM/weight consumption be paid for at less than its real cost. Because `SwapFirstAssetTrader` is used by Asset Hub runtimes to accept non-native assets for XCM fees (confirmed by `prdoc/stable2506/pr_8376.prdoc`), this is a live-scope runtime economic-integrity issue, not a theoretical one.

### Likelihood Explanation
No privileged role, relayer, validator, or leaked key is required — a single unprivileged account with enough capital (which can itself be flash-borrowed from within the same runtime via other DeFi-style primitives, or simply looped/self-funded) can construct the atomic sequence using ordinary, permissionless extrinsics/XCM instructions (`swap_exact_tokens_for_tokens`, `pallet_xcm::execute`). The precondition is simply that a shallow-enough `pallet-asset-conversion` pool exists between the fee `Target` asset and the asset the attacker wants to pay with, which is expected for smaller/newly-listed pools on Asset Hub.

### Recommendation
Do not price `SwapFirstAssetTrader`'s fee purchase off the pool's instantaneous reserves alone. Options:
- Require a time-weighted or previous-block-anchored reference price (comparable to a TWAP) for weight/fee quoting instead of `QuotePrice`'s spot computation.
- Reject `buy_weight`/`quote_weight` calls whose implied price deviates beyond a bound from a governance-set reference exchange rate.
- Disallow combining `pallet-asset-conversion` swap extrinsics with XCM fee-paying execution in the same atomic transaction/block for the same pool (e.g. via a mutex/one-swap-per-block-per-pool style guard), analogous to reentrancy/flash-loan guards.

### Proof of Concept
1. Attacker funds a pool `(swap_asset, Target)` thinly (or uses an existing thin pool).
2. In one transaction, attacker calls `pallet_asset_conversion::swap_exact_tokens_for_tokens` to shift `reserve(swap_asset)` down / `reserve(Target)` up.
3. In the same transaction, attacker submits `pallet_xcm::execute` with a message requiring `BuyExecution` in `swap_asset`; `SwapFirstAssetTrader::buy_weight` ( [6](#0-5) ) calls `SwapCredit::swap_tokens_for_exact_tokens` against the now-skewed reserves, taking far less `swap_asset` than the pre-manipulation price would require for the requested `fee` (`Target`) amount.
4. Attacker performs a reverse swap restoring reserves, recovering the bulk of the asset spent in step 2 (minus the ~0.3% AMM fee), completing the cycle in one atomic transaction with no lasting capital exposure — mirroring the "borrow, redeem, repay" sequence in the original Compound report.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L425-489)
```rust
	fn buy_weight(
		&mut self,
		weight: Weight,
		mut payment: AssetsInHolding,
		_context: &XcmContext,
	) -> Result<AssetsInHolding, (AssetsInHolding, XcmError)> {
		log::trace!(
			target: "xcm::weight",
			"SwapFirstAssetTrader::buy_weight weight: {:?}, payment: {:?}",
			weight,
			payment,
		);
		let Some((id, given_credit)) = payment.fungible.first_key_value() else {
			return Err((payment, XcmError::AssetNotFound));
		};
		let id = id.clone();
		let given_credit_amount = given_credit.amount();
		let first_asset: Asset = (id.clone(), given_credit_amount).into();
		let Ok((fungibles_id, _)) = FungiblesAssetMatcher::matches_fungibles(&first_asset) else {
			log::trace!(
				target: "xcm::weight",
				"SwapFirstAssetTrader::buy_weight asset {:?} didn't match",
				first_asset,
			);
			return Err((payment, XcmError::AssetNotFound));
		};

		let swap_asset = fungibles_id.clone().into();
		if Target::get().eq(&swap_asset) {
			log::trace!(
				target: "xcm::weight",
				"SwapFirstAssetTrader::buy_weight Asset was same as Target, swap not needed.",
			);
			// current trader is not applicable.
			return Err((payment, XcmError::FeesNotMet));
		}
		// Subtract required from payment
		let Some(imbalance) = payment.fungible.remove(&first_asset.id) else {
			return Err((payment, XcmError::TooExpensive));
		};
		// "manually" build the concrete credit and move the imbalance there.
		let mut credit_in = fungibles::Credit::<AccountId, Fungibles>::zero(fungibles_id);
		credit_in.saturating_subsume(imbalance);

		let fee = WeightToFee::weight_to_fee(&weight);
		// swap the user's asset for the `Target` asset.
		let (credit_out, credit_change) = match SwapCredit::swap_tokens_for_exact_tokens(
			vec![swap_asset, Target::get()],
			credit_in,
			fee,
		) {
			Ok(a) => a,
			Err((credit_in, error)) => {
				log::trace!(
					target: "xcm::weight",
					"SwapFirstAssetTrader::buy_weight swap couldn't be done. Error was: {:?}",
					error,
				);
				// put back the taken credit
				let taken =
					AssetsInHolding::new_from_fungible_credit(id.clone(), Box::new(credit_in));
				payment.subsume_assets(taken);
				return Err((payment, XcmError::FeesNotMet));
			},
		};
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L459-462)
```rust
		/// NOTE: when encountering an incorrect exchange rate and non-withdrawable pool liquidity,
		/// batch an atomic call with [`Pallet::add_liquidity`] and
		/// [`Pallet::swap_exact_tokens_for_tokens`] or [`Pallet::swap_tokens_for_exact_tokens`]
		/// calls to render the liquidity withdrawable and rectify the exchange rate.
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1514)
```rust
		pub fn get_reserves(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
		) -> Result<(T::Balance, T::Balance), Error<T>> {
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			let balance1 = Self::get_balance(&pool_account, asset1);
			let balance2 = Self::get_balance(&pool_account, asset2);

			if balance1.is_zero() || balance2.is_zero() {
				Err(Error::<T>::PoolEmpty)?;
			}

			Ok((balance1, balance2))
		}
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```
