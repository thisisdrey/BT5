Confirmed live usage: `SwapFirstAssetTrader` is actually configured as a `WeightTrader` in `asset-hub-rococo`, `asset-hub-westend`, `penpal`, and the staking-async parachain runtime's `xcm_config.rs`, meaning this is a real production XCM fee-payment path, not a dead/test-only utility.

### Title
Unbounded AMM slippage in XCM weight-fee swap allows sandwich extraction of user assets - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader::buy_weight` pays XCM execution fees by swapping the first fungible asset in the incoming holding for the runtime's `Target` fee asset via `SwapCredit::swap_tokens_for_exact_tokens`. The call fixes only the desired output (`fee`) and passes no `amount_in_max`, so the amount of the user's asset consumed is whatever the on-chain AMM pool (`pallet-asset-conversion`) computes from its current reserves at execution time, with no cap. This is the structural analog of the StargatePlugin bug: a swap invoked without a caller-supplied slippage bound, letting anyone who can move the pool's reserves before the swap executes extract value from the party being swapped (here, the XCM message's asset holding) instead of a sandwiched trader.

### Finding Description
In `buy_weight`, the trader takes the full `given_credit_amount` of the client asset out of the incoming holding and swaps it via: [1](#0-0) 

`SwapCredit::swap_tokens_for_exact_tokens` (the trait signature) intentionally has **no `amount_in_max` parameter at all**, unlike the pallet's public extrinsic `swap_tokens_for_exact_tokens` and its `Swap` trait counterpart, both of which do accept an optional max-input bound: [2](#0-1) [3](#0-2) 

The concrete pallet implementation of `do_swap_credit_tokens_for_exact_tokens` computes `amount_in` purely from the pool's live reserves via `balance_path_from_amount_out`, with no upper bound check on how much of `credit_in` gets consumed — the only protection is that `credit_in` itself must be sufficient: [4](#0-3) 

Because `pallet-asset-conversion` pools are public, permissionless constant-product AMMs, any unprivileged account can submit ordinary extrinsics (`add_liquidity`/`remove_liquidity`/`swap_exact_tokens_for_tokens`) in the same or a preceding block to shift a pool's reserves immediately before an XCM message triggers `buy_weight`, then reverse the position afterward. Since `buy_weight`'s swap has no `amount_in_max`, the fee-swap will silently pay whatever elevated price the manipulated pool dictates, and the attacker captures the difference through their own compensating trade. This mirrors the StargatePlugin flaw exactly: a swap function invoked with an unconstrained execution price, exploitable by manipulating the reference market immediately around the victim's transaction.

`SwapFirstAssetTrader` is not test-only scaffolding — it is wired in as the configured `WeightTrader` in `asset-hub-rococo`, `asset-hub-westend`, `penpal`, and `staking-async` parachain runtimes' `xcm_config.rs`, meaning any incoming XCM program that pays fees in a non-native asset through this trader is exposed.

### Impact Explanation
Value is extracted from XCM message senders (their fee-asset holding is debited more than a fair, non-manipulated price would require) and captured by whoever manipulates the pool reserves around the fee swap. Because this executes inside core XCM weight-fee accounting on Asset Hub / Penpal, it degrades the intended fee-payment invariant (users should pay a fair market-rate fee) and allows a public, unprivileged actor to systematically skim value from cross-chain message senders using non-native fee assets — a form of public underpriced/overpriced-work extraction on a widely deployed runtime component.

### Likelihood Explanation
The attack requires only ordinary, permissionless `pallet-asset-conversion` extrinsics (swap/add-liquidity/remove-liquidity) that any account can call, executed around the target XCM message's inclusion in the same parachain block — no validator, collator, relayer, or governance privilege is needed. Profitability depends on pool depth/fee-in-asset volume, but the guard that would prevent it (an `amount_in_max`/slippage bound) is structurally absent from the `SwapCredit::swap_tokens_for_exact_tokens` trait used here, unlike every other swap entrypoint in the same pallet.

### Recommendation
Add an `amount_in_max` (or equivalent slippage bound) parameter to `SwapCredit::swap_tokens_for_exact_tokens`, and have `SwapFirstAssetTrader::buy_weight` compute an acceptable bound (e.g., via `QuotePrice::quote_price_tokens_for_exact_tokens` taken just before the swap, with a tolerance) and pass it through, failing the trade with `XcmError::TooExpensive` if the live pool price has moved beyond that bound — mirroring the max-input protections already present in the pallet's public `swap_tokens_for_exact_tokens` extrinsic and `Swap` trait.

### Proof of Concept
1. Attacker observes an incoming XCM message that will pay execution fees in `AssetX` via `SwapFirstAssetTrader` configured with `Target = DOT` on Asset Hub.
2. In the same block, before the message executes, attacker submits `AssetConversion::swap_exact_tokens_for_tokens` (or manipulates liquidity) to spike the `AssetX/DOT` pool price against `AssetX`.
3. The XCM executor invokes `buy_weight`, which calls `SwapCredit::swap_tokens_for_exact_tokens([AssetX, DOT], credit_in, fee)`. Because no `amount_in_max` bound exists, the call succeeds at the manipulated rate, consuming a disproportionately large amount of the message's `AssetX` holding for the fixed `fee` in `DOT`.
4. Attacker immediately reverses their pool-manipulating trade, restoring the price and pocketing the extra `AssetX` value extracted from the victim's fee payment as arbitrage profit, exactly as in the StargatePlugin sandwich scenario but against XCM fee payers instead of DEX swappers.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-489)
```rust
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L62-69)
```rust
	fn swap_tokens_for_exact_tokens(
		sender: AccountId,
		path: Vec<Self::AssetKind>,
		amount_out: Self::Balance,
		amount_in_max: Option<Self::Balance>,
		send_to: AccountId,
		keep_alive: bool,
	) -> Result<Self::Balance, DispatchError>;
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L99-113)
```rust
	/// Swaps a portion of `credit_in` of `path[0]` asset to obtain the desired `amount_out` of
	/// the `path[last]` asset. The provided `credit_in` must be adequate to achieve the target
	/// `amount_out`, or an error will occur.
	///
	/// On success, the function returns a (`credit_out`, `credit_change`) tuple, where `credit_out`
	/// represents the acquired amount of the `path[last]` asset, and `credit_change` is the
	/// remaining portion from the `credit_in`. On failure, an `Err` with the initial `credit_in`
	/// and error code is returned.
	///
	/// This operation is expected to be atomic.
	fn swap_tokens_for_exact_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out: Self::Balance,
	) -> Result<(Self::Credit, Self::Credit), (Self::Credit, DispatchError)>;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1065-1109)
```rust
		/// Swap exactly `credit_in` of asset `path[0]` for asset `path[last]`.  If `amount_out_min`
		/// is provided and the swap can't achieve at least this amount, an error is returned.
		///
		/// On a successful swap, the function returns the `credit_out` of `path[last]` obtained
		/// from the `credit_in`. On failure, it returns an `Err` containing the original
		/// `credit_in` and the associated error code.
		///
		/// WARNING: This may return an error after a partial storage mutation. It should be used
		/// only inside a transactional storage context and an Err result must imply a storage
		/// rollback.
		pub(crate) fn do_swap_exact_credit_tokens_for_tokens(
			path: Vec<T::AssetKind>,
			credit_in: CreditOf<T>,
			amount_out_min: Option<T::Balance>,
		) -> Result<CreditOf<T>, (CreditOf<T>, DispatchError)> {
			let amount_in = credit_in.peek();
			let inspect_path = |credit_asset| {
				ensure!(
					path.first().map_or(false, |a| *a == credit_asset),
					Error::<T>::InvalidPath
				);
				ensure!(!amount_in.is_zero(), Error::<T>::ZeroAmount);
				ensure!(amount_out_min.map_or(true, |a| !a.is_zero()), Error::<T>::ZeroAmount);

				Self::validate_swap_path(&path)?;
				let path = Self::balance_path_from_amount_in(amount_in, path)?;

				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
				Ok((path, amount_out))
			};
			let (path, amount_out) = match inspect_path(credit_in.asset()) {
				Ok((p, a)) => (p, a),
				Err(e) => return Err((credit_in, e)),
			};

			let credit_out = Self::credit_swap(credit_in, &path)?;

			Self::deposit_event(Event::SwapCreditExecuted { amount_in, amount_out, path });

			Ok(credit_out)
		}
```
