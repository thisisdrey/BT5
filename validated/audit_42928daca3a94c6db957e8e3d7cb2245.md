## Analysis

The Curve report's core invariant is: *a permissionless, user-controlled price source (an "oracle") is trusted by protocol logic to value assets, with no whitelist or manipulation resistance, letting the party who controls that price source benefit at the expense of the protocol/other users.*

The direct local analog is `pallet_asset_conversion`, whose AMM pools double as the on-chain "price oracle" consumed by `pallet_asset_conversion_tx_payment` to price transaction fees paid in non-native assets. Pool creation is fully permissionless and unwhitelisted, and the fee-payment logic reads the pool's *instantaneous spot reserves* — not a manipulation-resistant price — at a point in the transaction lifecycle that the transaction's own dispatched call can influence.

### Title
Permissionless, unwhitelisted AssetConversion pools let a tx signer manipulate their own pool's spot price to distort fee refunds - (File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs)

### Summary
`pallet_asset_conversion::create_pool` lets any signed account create a liquidity pool for an arbitrary asset pair with no admin/whitelist check, only payment of a setup fee [1](#0-0) . `pallet_asset_conversion_tx_payment::SwapAssetAdapter` uses this same pool's live reserves as a de-facto price oracle to convert a user's chosen fee-asset into the native fee amount, both before dispatch (`withdraw_fee`) and — critically — after dispatch (`correct_and_deposit_fee`), where the refund is computed from the pool's post-call spot price [2](#0-1) . Because the pool is self-created/self-funded by the same account paying the fee, and the dispatched call within the *same extrinsic* can itself swap against that pool, the signer can move the spot price between fee withdrawal and fee correction to their advantage.

### Finding Description
`do_create_pool` performs no whitelist or reputation check on the asset pair or creator — any `ensure_signed` origin can register a pool for any `AssetKind` pair as long as the setup fee is paid [3](#0-2) . This is exactly the unwhitelisted "user-supplied oracle" pattern the external report warns about: nothing prevents a single account from being creator, sole liquidity provider, and sole user of a pool.

`ChargeAssetTxPayment` lets the *transaction signer* pick which `asset_id` to pay fees in [4](#0-3) . `withdraw_fee` quotes `asset_fee` from the pool's reserves *before* the call executes. After dispatch, `correct_and_deposit_fee` computes the refund by quoting `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id, refund_amount, true)` — i.e., it re-reads the *same pool's* spot reserves, but now *after* the dispatched call has run [5](#0-4) . `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` are explicitly documented as spot quotes with no manipulation resistance: "the price may have changed by the time the transaction is executed" [6](#0-5) .

Because the pool used for pricing is the attacker's own (self-created, permissionless, unwhitelisted) pool, and the `RuntimeCall` being dispatched in the very same extrinsic is attacker-controlled, the attacker can embed a swap (or add/remove liquidity) against that pool as part of the dispatched call so that by the time `correct_and_deposit_fee` reads reserves, the exchange rate has moved sharply in their favor. `withdraw_fee`'s pre-dispatch quote used the *original* (unmanipulated) reserves to determine `fee_asset_amount` withdrawn, while the *refund* is computed off the post-manipulation reserves — the two quotes are inconsistent because the state moved between them, and nothing enforces atomic/consistent pricing or bounds the discrepancy.

### Impact Explanation
This directly maps to the "public underpriced work that degrades block production" impact category: a user can construct a transaction that consumes real chain weight/bandwidth (the dispatched call) while paying an artificially reduced *net* fee, because the refund leg of the fee logic is priced from a spot value the same transaction just manipulated. At scale this allows spamming underpriced transactions against a self-owned, tiny-liquidity pool, degrading block production economics without needing any privileged role, validator, collator, or off-chain oracle compromise — purely a public extrinsic path.

### Likelihood Explanation
High for a sophisticated but entirely unprivileged actor: pool creation is public and cheap (one setup fee), the attacker fully controls both the pool's initial liquidity and the manipulating call, and `pallet_asset_conversion` explicitly does not offer TWAP/manipulation-resistant pricing — only instantaneous reserve ratios. No governance, admin, relayer, or validator collusion is required; it is reachable purely through `AssetConversion::create_pool`/`add_liquidity` plus a normal signed extrinsic using `ChargeAssetTxPayment`.

### Recommendation
Do not use raw AMM spot reserves from arbitrary, permissionlessly created pools as the price source for protocol-critical accounting such as fee correction. Either: (1) restrict which pools/assets are eligible for `ChargeAssetTxPayment` fee payment via a whitelist (mirroring the external report's recommendation for oracle whitelisting), (2) require the *same* quoted reserve state be used consistently for both withdrawal and refund (e.g., cache the pre-dispatch reserves/quote and use it for the refund direction too, or disallow refund entirely when the fee-asset pool state changed during dispatch), or (3) require a time/volume-weighted price rather than instantaneous spot reserves for any AssetConversion pool used in fee-conversion paths.

### Proof of Concept
1. Attacker calls `AssetConversion::create_pool(asset_X, Native)` and `add_liquidity` with a small amount, becoming sole LP of a thin `asset_X`/`Native` pool.
2. Attacker submits a signed extrinsic with `ChargeAssetTxPayment::from(tip, Some(asset_X))` and a `RuntimeCall` that is itself a large/expensive `AssetConversion::swap_exact_tokens_for_tokens` (or `remove_liquidity`) against the `asset_X`/`Native` pool, shifting its reserve ratio dramatically.
3. `withdraw_fee` quotes and withdraws `fee_asset_amount` of `asset_X` using the pre-swap reserves.
4. The dispatched call executes, moving the pool's reserves.
5. `correct_and_deposit_fee` computes `refund_asset_amount` via `quote_price_exact_tokens_for_tokens` using the now-manipulated reserves, producing a refund inconsistent with the true economic value withdrawn — net fee paid for the block space consumed can be made near-zero while the weight/length cost was real.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L729-751)
```rust
		pub(crate) fn do_create_pool(
			creator: &T::AccountId,
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			initial_fee: Option<Permill>,
		) -> Result<T::PoolId, DispatchError> {
			ensure!(asset1 != asset2, Error::<T>::InvalidAssetPair);
			if let Some(fee) = initial_fee {
				ensure!(fee <= T::MaxSwapFee::get(), Error::<T>::FeeTooHigh);
			}

			// prepare pool_id
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			// pay the setup fee
			let fee =
				Self::withdraw(T::PoolSetupFeeAsset::get(), creator, T::PoolSetupFee::get(), true)?;
			T::PoolSetupFeeTarget::on_unbalanced(fee);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1516-1522)
```rust
		/// Gets a quote for swapping an exact amount of `asset1` for `asset2`.
		///
		/// If `include_fee` is true, the quote will include the liquidity provider fee.
		/// If the pool does not exist or has no liquidity, `None` is returned.
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_out_min` to control slippage.)
		/// Returns `Some(quoted_amount)` on success.
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L119-146)
```rust
	fn withdraw_fee(
		who: &T::AccountId,
		_call: &T::RuntimeCall,
		_dispatch_info: &DispatchInfoOf<<T>::RuntimeCall>,
		asset_id: Self::AssetId,
		fee: Self::Balance,
		_tip: Self::Balance,
	) -> Result<Self::LiquidityInfo, TransactionValidityError> {
		if asset_id == A::get() {
			// The `asset_id` is the target asset, we do not need to swap.
			let fee_credit = F::withdraw(
				asset_id.clone(),
				who,
				fee,
				Precision::Exact,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.map_err(|_| InvalidTransaction::Payment)?;

			return Ok((fee_credit, fee));
		}

		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-280)
```rust
		// refund is non zero and `who`'s fee `asset_id` is not the target asset.

		// check if the refund amount can be swapped back into `who`'s fee `asset_id`.
		let refund_asset_amount =
			S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
				// No refund given if it cannot be swapped back.
				.unwrap_or(Zero::zero());

		// `fee_paid` cannot be swapped back into `who`'s fee `asset_id` or the refund amount cannot
		// be deposited into `who`'s fee `asset_id`, exit without refund.
		if refund_asset_amount.is_zero() ||
			!matches!(
				F::can_deposit(asset_id.clone(), who, refund_asset_amount, Provenance::Extant),
				DepositConsequence::Success
			) {
			let (tip, fee) = fee_paid.split(tip);
			OU::on_unbalanceds(Some(fee).into_iter().chain(Some(tip)));
			return Ok(fee_asset_amount);
		}

		// swap the refund amount back into `who`'s fee `asset_id`.

```
