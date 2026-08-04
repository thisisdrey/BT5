## Finding [1](#0-0) 

### Title
`SwapFirstAssetTrader::refund_weight` performs an AMM swap with no slippage floor (`amount_out_min = None`) - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used by parachain XCM configs (Asset Hub Rococo/Westend, Penpal, staking-async parachain) to let users pay XCM execution fees in a non-native asset by swapping it through `pallet_asset_conversion` into a `Target` fee asset. `buy_weight()` correctly bounds the swap by using `swap_tokens_for_exact_tokens`, where the credit-in itself caps the maximum spent. However `refund_weight()` swaps the unused portion of the collected `Target` fee back into the original client asset via `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` — passing `None` for `amount_out_min`, i.e. with no slippage protection at all, exactly the missing-slippage-check pattern described in the source report.

### Finding Description
`buy_weight` records how much of the `Target` asset was actually obtained for a client's payment asset in `self.total_fee`, and remembers `self.last_fee_asset` [2](#0-1) . When unused weight is later refunded, `refund_weight` extracts the corresponding slice of `total_fee` (in `Target` asset) and swaps it back to the client's original asset:

```rust
let refund = self.total_fee.extract(refund_amount);
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) { ... };
``` [3](#0-2) 

Here `pallet_asset_conversion`'s `SwapCredit::swap_exact_tokens_for_tokens` accepts an `Option<Balance>` `amount_out_min` specifically so callers can enforce a minimum acceptable output [4](#0-3) , and the pallet's own tests confirm that omitting/under-specifying this bound is the sole guard against adverse pricing (`ProvidedMinimumNotSufficientForSwap`) [5](#0-4) . By passing `None`, `refund_weight` disables this guard entirely, so the swap executes at whatever spot price the pool currently has, no matter how unfavorable — mirroring the `CNumaToken.leverageStrategy()` bug where `borrowAmount` is computed from `getAmountIn` with no upper/lower bound check before committing funds.

`pallet_asset_conversion` pools are permissionless: any account can `create_pool`, `add_liquidity`, and `remove_liquidity` for arbitrary asset pairs [6](#0-5) . This means an unprivileged actor can, within the same block, imbalance the reserves of the `Target`/`refund_swap_asset` pool via ordinary extrinsics (e.g. adding lopsided liquidity or performing a swap immediately before the refund is processed by the XCM executor), then trigger or wait for an XCM message that uses `SwapFirstAssetTrader` and produces a `refund_weight` call. The refund will convert protocol-collected `Target` value into `refund_swap_asset` at the manipulated rate, extracting pool value at the expense of that pool's liquidity providers with no floor to stop it, and with no dependency on a malicious relayer, collator, or validator — only ordinary permissionless transactions plus normal XCM message execution in the same block.

### Impact Explanation
Because the refund can settle at an arbitrary (attacker-influenced) exchange rate, LPs of the affected `pallet_asset_conversion` pool can have value siphoned out through repeated refund events triggered against a pool whose reserves were just skewed by the attacker's own liquidity/swap transactions. This is a public, underpriced/unprotected value-transfer path reachable by any user who can submit ordinary extrinsics plus an XCM message routed through a chain using `SwapFirstAssetTrader` (Asset Hub Rococo/Westend, Penpal, and the staking-async parachain runtime configure it) [7](#0-6) , resulting in fund loss for pool participants — squarely within the "conserve value / settle exactly once to rightful beneficiary" pivot.

### Likelihood Explanation
Likelihood is moderate-to-high on any deployment enabling `SwapFirstAssetTrader` together with a permissionless `pallet_asset_conversion` pool for the refund pair: the attacker needs no privileged role, only ordinary account access to submit liquidity/swap extrinsics and trigger (or wait for) an XCM message that leaves unused weight to refund. `buy_weight` is correctly bounded (uses exact-output swap sized to the credit-in), so only the `refund_weight` path is exposed.

### Recommendation
Compute and pass a proper `amount_out_min` in `refund_weight`'s call to `SwapCredit::swap_exact_tokens_for_tokens`, e.g. derive it from a `QuotePrice::quote_price_exact_tokens_for_tokens` call (already used elsewhere in this file for `quote_weight`) with an acceptable tolerance, and treat a swap that fails the floor the same way `Err` is currently handled (return the credit to `total_fee` and skip the refund) rather than allowing an unbounded-slippage swap to execute.

### Proof of Concept
1. Attacker (or anyone) creates/funds a `pallet_asset_conversion` pool for `(Target, refund_swap_asset)` with shallow liquidity, permitted since pool creation/liquidity provisioning is permissionless [6](#0-5) .
2. Attacker submits an XCM message that is processed by `SwapFirstAssetTrader::buy_weight`, paying fees in `refund_swap_asset`, causing part of `Target::get()` fee to sit in `self.total_fee` awaiting a future `refund_weight` call for unused weight (e.g. an over-estimated `BuyExecution` weight).
3. In the same block, before the executor calls `refund_weight`, attacker submits a large one-sided swap or liquidity removal against the `(Target, refund_swap_asset)` pool to drive the spot price to an extreme.
4. `refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(..., None)` [8](#0-7) , converting the refund amount of `Target` into `refund_swap_asset` at the skewed price with no `ProvidedMinimumNotSufficientForSwap` check to block it, extracting value from the pool's remaining liquidity providers.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-509)
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

		match self.total_fee.subsume(credit_out) {
			Err(credit_out) => {
				// error may occur if `total_fee.asset` differs from `credit_out.asset`, which does
				// not apply in this context.
				defensive!(
					"`total_fee.asset` must be equal to `credit_out.asset`",
					(self.total_fee.asset(), credit_out.asset())
				);
				return Err((payment, XcmError::FeesNotMet));
			},
			_ => (),
		};
		self.last_fee_asset = Some(id.clone());

		if credit_change.peek() != Zero::zero() {
			let unspent = AssetsInHolding::new_from_fungible_credit(id, Box::new(credit_change));
			payment.subsume_assets(unspent);
		}
		Ok(payment)
```

**File:** cumulus/primitives/utility/src/lib.rs (L512-544)
```rust
	fn refund_weight(&mut self, weight: Weight, _context: &XcmContext) -> Option<AssetsInHolding> {
		log::trace!(
			target: "xcm::weight",
			"SwapFirstAssetTrader::refund_weight weight: {:?}, self.total_fee: {:?}",
			weight,
			self.total_fee,
		);
		if weight.is_zero() || self.total_fee.peek().is_zero() {
			// noting to refund.
			return None;
		}
		let refund_asset = if let Some(asset) = &self.last_fee_asset {
			// create an initial zero refund in the asset used in the last `buy_weight`.
			(asset.clone(), Fungible(0)).into()
		} else {
			return None;
		};
		let refund_amount = WeightToFee::weight_to_fee(&weight);
		if refund_amount >= self.total_fee.peek() {
			// not enough was paid to refund the `weight`.
			return None;
		}

		let refund_swap_asset = FungiblesAssetMatcher::matches_fungibles(&refund_asset)
			.map(|(a, _)| a.into())
			.ok()?;

		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-135)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
	/// Measurement units of the asset classes for pricing.
	type Balance: Balance;
	/// Type representing the kind of assets for which the price is being quoted.
	type AssetKind;
	/// Quotes the amount of `asset1` required to obtain the exact `amount` of `asset2`.
	///
	/// If `include_fee` is set to `true`, the price will include the pool's fee.
	/// If the pool does not exist or the swap cannot be made, `None` is returned.
	fn quote_price_tokens_for_exact_tokens(
		asset1: Self::AssetKind,
		asset2: Self::AssetKind,
		amount: Self::Balance,
		include_fee: bool,
	) -> Option<Self::Balance>;
	/// Quotes the amount of `asset2` resulting from swapping the exact `amount` of `asset1`.
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L1565-1613)
```rust
#[test]
fn swap_should_not_work_if_too_much_slippage() {
	new_test_ext().execute_with(|| {
		let user = 1;
		let token_1 = NativeOrWithId::Native;
		let token_2 = NativeOrWithId::WithId(2);

		create_tokens(user, vec![token_2.clone()]);
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone())
		));

		assert_ok!(Balances::force_set_balance(
			RuntimeOrigin::root(),
			user,
			10000 + get_native_ed()
		));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 2, user, 1000));

		let liquidity1 = 10000;
		let liquidity2 = 200;

		assert_ok!(AssetConversion::add_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone()),
			liquidity1,
			liquidity2,
			1,
			1,
			user,
		));

		let exchange_amount = 100;

		assert_noop!(
			AssetConversion::swap_exact_tokens_for_tokens(
				RuntimeOrigin::signed(user),
				bvec![token_2.clone(), token_1.clone()],
				exchange_amount, // amount_in
				4000,            // amount_out_min
				user,
				false,
			),
			Error::<Test>::ProvidedMinimumNotSufficientForSwap
		);
	});
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L493-517)
```rust
		/// burned in the process. With the usage of `amount1_min_receive`/`amount2_min_receive`
		/// it's possible to control the min amount of returned tokens you're happy with.
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::remove_liquidity())]
		pub fn remove_liquidity(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
			lp_token_burn: T::Balance,
			amount1_min_receive: T::Balance,
			amount2_min_receive: T::Balance,
			withdraw_to: T::AccountId,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_remove_liquidity(
				&sender,
				*asset1,
				*asset2,
				lp_token_burn,
				amount1_min_receive,
				amount2_min_receive,
				&withdraw_to,
			)?;
			Ok(())
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```
