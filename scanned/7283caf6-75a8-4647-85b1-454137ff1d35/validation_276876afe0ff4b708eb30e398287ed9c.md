### Title
`swap_and_burn` performs the tip/fee-asset-to-Ether swap with `amount_out_min = None`, exposing `register_token`/`add_tip` callers to unbounded price-impact loss - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`pallet_snowbridge_system_frontend`'s `register_token` and `add_tip` extrinsics let *any* signed origin pay Snowbridge fees/tips in an arbitrary asset, which the pallet then swaps into Ether via `pallet_asset_conversion::Swap::swap_exact_tokens_for_tokens` before burning it for teleport to Ethereum. The call site explicitly passes `None` for the slippage bound, with an inline comment "No minimum amount required." [1](#0-0) 

### Finding Description
`swap_and_burn` performs the conversion of a caller-supplied `fee_asset`/tip asset into Ether: [2](#0-1) 

The `amount_out_min` argument of `Swap::swap_exact_tokens_for_tokens` — the exact mechanism `pallet-asset-conversion` provides for slippage protection (`ProvidedMinimumNotSufficientForSwap`) — is hard-coded to `None`. This is invoked from two public, unprivileged, signed-origin extrinsics:

- `register_token`, which swaps `fee_asset` for Ether whenever the caller is not the root location [3](#0-2) 
- `add_tip`, which swaps any signed caller's tip `asset` for Ether unconditionally [4](#0-3) 

Both funnel through `swap_fee_asset_and_burn` → `swap_and_burn`, which resolves the AMM pool spot price at execution time with zero floor on the output. Contrast this with `pallet-asset-conversion`'s own extrinsics and even the `SwapAssetAdapter`/`SwapFirstAssetTrader` used for transaction-fee payment, which always quote or bound the swap (`quote_price_tokens_for_exact_tokens`, exact-amount swaps, or a `Some(min)` argument) before committing funds: [5](#0-4) 

This mirrors the GMX/M-28 pattern: the code has a slippage-protection mechanism (`amount_out_min`) but a specific execution path in the codebase forgoes it entirely, so the amount of Ether actually credited/burned (`ether_gained`) can be arbitrarily lower than the fair-market value of the input asset the user surrendered, and there is no way — not even optionally — for the caller to bound the loss. `ether_gained` is then used directly as the registration/tip amount forwarded to BridgeHub, so the burned/consumed value is whatever the manipulated pool state produces, with no on-chain check that it's reasonable.

### Impact Explanation
Any user calling `register_token` or `add_tip` with a non-Ether asset has their asset unconditionally exchanged at whatever price the `AssetConversion` pool reports at execution time, with no protection against price impact from thin liquidity, prior same-block trades, or an adversary who moves the pool price before this extrinsic executes in the block. This directly causes the caller to lose value (get less Ether burned/registered than the fair value of what they supplied) with no recourse, and there is no parameter in the extrinsic to guard against it — a systemic, always-on underpricing risk rather than a one-off user mistake. This falls under "runtime bugs that compromise intended behavior" and value-conservation guarantees for bridge fee/reward accounting (the swap should convert the tip to its fair Ether value before burning/forwarding it for the Ethereum-side reward).

### Likelihood Explanation
The path is reachable by any signed account with no privilege needed via two straightforward public extrinsics (`register_token` when not called as the "root" location, and `add_tip` for any signed origin) whenever the pool for `(fee_asset, Ether)` has non-trivial spread or low depth — a routine condition for newly listed or thinly-traded assets. No malicious peer, relayer, validator, or governance actor is required; the loss can be caused simply by trading against a pool with adverse price impact.

### Recommendation
Add a `min_ether_out`/slippage-bound parameter to `register_token`/`add_tip` (or derive a safe minimum internally via `QuotePrice::quote_price_exact_tokens_for_tokens` with an acceptable tolerance) and pass `Some(min_ether_out)` into `Swap::swap_exact_tokens_for_tokens` in `swap_and_burn`, mirroring how `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter` always quotes/bounds swaps before committing user funds.

### Proof of Concept
1. A pool exists for `(TipAsset, Ether)` in `pallet-asset-conversion` with modest liquidity.
2. Attacker (or ordinary price movement/thin liquidity) shifts the pool's spot price unfavorably for `TipAsset → Ether` immediately before/within the same block as the victim's `add_tip` (or `register_token`) call.
3. Victim calls `add_tip(message_id, Asset{ id: TipAsset, fun: Fungible(tip_amount) })`.
4. `swap_and_burn` executes `T::Swap::swap_exact_tokens_for_tokens(who, [TipAsset, Ether], tip_amount, None, who, true)` — see [6](#0-5) . Because `amount_out_min` is `None`, the swap succeeds even though `ether_gained` is far below the fair value of `tip_amount`.
5. `ether_gained` is burned for teleport and forwarded as the tip amount to BridgeHub — the victim has permanently lost the difference between fair value and the manipulated execution price, with no on-chain guard (unlike `pallet-asset-conversion`'s own `swap_exact_tokens_for_tokens` extrinsic, which would have reverted with `ProvidedMinimumNotSufficientForSwap` had a minimum been supplied).

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-252)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};

			let call = Self::build_register_token_call(
				origin_location.clone(),
				asset_location,
				metadata,
				ether_gained,
			)?;

			Self::send_transact_call(origin_location, call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L290-317)
```rust
		fn swap_and_burn(
			origin: Location,
			tip_asset_location: Location,
			ether_location: Location,
			tip_amount: u128,
		) -> Result<u128, DispatchError> {
			// Swap tip asset to ether
			let swap_path = vec![tip_asset_location.clone(), ether_location.clone()];
			let who = T::AccountIdConverter::convert_location(&origin)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;

			// Burn the ether
			let ether_asset = Asset::from((ether_location.clone(), ether_gained));

			burn_for_teleport::<T::AssetTransactor>(&origin, &ether_asset)
				.map_err(|_| Error::<T>::BurnError)?;

			Ok(ether_gained)
		}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-176)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;

		// Withdraw the `asset_id` credit for the swap.
		let asset_fee_credit = F::withdraw(
			asset_id.clone(),
			who,
			asset_fee,
			Precision::Exact,
			Preservation::Preserve,
			Fortitude::Polite,
		)
		.map_err(|_| InvalidTransaction::Payment)?;

		let (fee_credit, change) = match S::swap_tokens_for_exact_tokens(
			vec![asset_id, A::get()],
			asset_fee_credit,
			fee,
		) {
			Ok((fee_credit, change)) => (fee_credit, change),
			Err((credit_in, _)) => {
				defensive!("Fee swap should pass for the quoted amount");
				let _ = F::resolve(who, credit_in).defensive_proof("Should resolve the credit");
				return Err(InvalidTransaction::Payment.into());
			},
		};

		// Since the exact price for `fee` has been quoted, the change should be zero.
		ensure!(change.peek().is_zero(), InvalidTransaction::Payment);

		Ok((fee_credit, asset_fee))
	}
```
