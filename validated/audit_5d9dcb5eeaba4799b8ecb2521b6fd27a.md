### Title
Zero-slippage AMM swap in `snowbridge-pallet-system-frontend` allows sandwich attack that drains user tip/fee assets and underfunds Ethereum-side execution - (File: bridges/snowbridge/pallets/system-frontend/src/lib.rs)

### Summary
`Pallet::swap_and_burn` (called from both the public `register_token` and `add_tip` extrinsics) invokes `T::Swap::swap_exact_tokens_for_tokens` with the minimum-output parameter hardcoded to `None`, explicitly disabling slippage protection on the AMM trade that converts a user-supplied fee/tip asset into Ether before it is burned for teleport to Ethereum. Any unprivileged actor can sandwich this swap against the public `pallet_asset_conversion` pool, extracting nearly all of the value while the resulting (near-zero) `ether_gained` is still used as the authoritative execution-fee/relayer-tip amount forwarded to BridgeHub and ultimately to Ethereum.

### Finding Description
`swap_and_burn` performs the swap like this: [1](#0-0) 

The comment `// No minimum amount required` confirms this is intentional but unsafe: there is no `amount_out_min` check, mirroring the `PendleAdapter.deposit` pattern in the external report where `minOut = 0` and slippage protection is delegated to a caller that never actually validates the resulting amount.

`ether_gained`, the raw output of this unprotected swap, is used directly as:
- the `amount` field of `EthereumSystemCall::RegisterToken`, which represents the tip/fee value transacted to BridgeHub for `register_token` [2](#0-1) 
- the `amount` field of `EthereumSystemCall::AddTip`, i.e. the relayer reward tip for a specific `message_id` [3](#0-2) 

Both `register_token` (for non-`Root` origins) and `add_tip` are open, permissionless entry points reachable by any signed account, and both call `swap_fee_asset_and_burn` → `swap_and_burn` whenever the supplied fee/tip asset is not already Ether: [4](#0-3) 

The swap is executed against `pallet_asset_conversion`'s public, permissionless liquidity pool via the generic `Swap` trait, whose signature explicitly supports an `amount_out_min: Option<Balance>` guard designed for exactly this purpose: [5](#0-4) 

Elsewhere in the codebase, the pallet's own extrinsic and `SwapCredit` paths correctly enforce this minimum (`ProvidedMinimumNotSufficientForSwap`), and the XCM `SingleAssetExchangeAdapter` also always passes a real minimum: [6](#0-5) [7](#0-6) 

`snowbridge-pallet-system-frontend` is the only caller in the repository that deliberately passes `None`, bypassing this protection entirely. Because Substrate transactions are visible in the transaction pool/block-builder queue before inclusion, any actor (no privileged role, node, validator, or relayer access required) can front-run a pending `register_token`/`add_tip` call with a large trade on the same pool leg (`tip_asset → ether`), then back-run it after the victim's swap executes, extracting essentially all of the swap's expected output as arbitrage profit. The victim's `ether_gained` collapses toward zero while their entire input asset has still been consumed and burned.

### Impact Explanation
Because `ether_gained` (or its underpriced remainder) is forwarded verbatim as the authoritative fee/tip amount to the BridgeHub-side `EthereumSystem::register_token`/`add_tip` extrinsics, a sandwiched call causes:
- `register_token`: the on-chain reported "execution fee" amount sent to Ethereum is artificially deflated, potentially causing Ethereum-side command execution to under-fund/fail while the token registration is still committed with an under-collateralized fee — degrading the bridge's guaranteed processing/settlement path.
- `add_tip`: the relayer reward tip attached to a given `message_id` is minted/allocated at a near-zero value even though the user paid the full input amount, i.e. the user's asset is destroyed (burned for teleport is the swap output, which is close to zero) for essentially no benefit, a direct value-loss/fund-loss condition tied to Snowbridge's reward/delivery flow.

This is a real, unbacked-value-loss and underpriced-work vector directly touching the Snowbridge delivery/reward accounting pivot in scope.

### Likelihood Explanation
Likelihood is Low-to-Medium: it requires the attacker to observe a pending `register_token`/`add_tip` transaction and execute a front-run/back-run pair within the same block window against the relevant `pallet_asset_conversion` pool — a standard, well-known MEV/sandwich technique that needs no privileged role, node operator, or validator collusion, matching the external report's "sandwich attack" primitive exactly. It is bounded by pool liquidity depth and the attacker's capital, which is why impact is rated High but likelihood Low, consistent with the original report's own severity classification.

### Recommendation
Add a real `amount_out_min` parameter to `register_token`/`add_tip` (or derive a conservative minimum via `pallet_asset_conversion::QuotePrice`/`quote_price_exact_tokens_for_tokens` at call time with an acceptable tolerance) and pass it into `T::Swap::swap_exact_tokens_for_tokens` instead of `None`, causing the swap to fail (and the extrinsic to revert) if the pool is manipulated beyond an acceptable slippage bound.

### Proof of Concept
1. Attacker monitors the transaction pool and sees a user's `add_tip(message_id, asset)` call with `asset = (tip_asset_location, 1000)`, targeting the `tip_asset → ether` pool in `pallet_asset_conversion`.
2. Attacker submits a large `swap_exact_tokens_for_tokens(tip_asset → ether)` trade immediately before the victim's transaction in the same block, moving the pool price so `ether` is expensive relative to `tip_asset`.
3. Victim's `add_tip` executes `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)` — since there is no minimum, the trade succeeds even though `ether_gained` is now far below the fair-market value of the original 1000 `tip_asset`.
4. Attacker immediately submits a reverse trade (`ether → tip_asset`) after the victim's transaction, restoring the pool price and capturing the price difference as profit.
5. `ether_gained` (now heavily discounted) is burned via `burn_for_teleport` and forwarded as the tip `amount` in `EthereumSystemCall::AddTip` to BridgeHub — the relayer reward recorded for `message_id` is far lower than the user intended/paid for, while the attacker has extracted the difference from the pool.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L237-252)
```rust
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L297-308)
```rust
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
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L372-403)
```rust
		fn swap_fee_asset_and_burn(
			origin: Location,
			fee_asset: Asset,
		) -> Result<u128, DispatchError> {
			let ether_location = T::EthereumLocation::get();
			let (fee_asset_location, fee_amount) = match fee_asset {
				Asset { id: AssetId(ref loc), fun: Fungible(amount) } => (loc, amount),
				_ => {
					tracing::debug!(target: LOG_TARGET, ?fee_asset, "error matching fee asset");
					return Err(Error::<T>::UnsupportedAsset.into());
				},
			};
			if fee_amount == 0 {
				return Ok(0);
			}

			let ether_gained = if *fee_asset_location != ether_location {
				Self::swap_and_burn(
					origin.clone(),
					fee_asset_location.clone(),
					ether_location,
					fee_amount,
				)
				.inspect_err(|&e| {
					tracing::debug!(target: LOG_TARGET, ?e, "error swapping asset");
				})?
			} else {
				burn_for_teleport::<T::AssetTransactor>(&origin, &fee_asset)
					.map_err(|_| Error::<T>::BurnError)?;
				fee_amount
			};
			Ok(ether_gained)
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L43-50)
```rust
	fn swap_exact_tokens_for_tokens(
		sender: AccountId,
		path: Vec<Self::AssetKind>,
		amount_in: Self::Balance,
		amount_out_min: Option<Self::Balance>,
		send_to: AccountId,
		keep_alive: bool,
	) -> Result<Self::Balance, DispatchError>;
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

**File:** polkadot/xcm/xcm-builder/src/asset_exchange/single_asset_adapter/adapter.rs (L106-114)
```rust
		// Do the swap.
		let (credit_out, maybe_credit_change) = if maximal {
			// If `maximal`, then we swap exactly `credit_in` to get as much of `want_asset_id` as
			// we can, with a minimum of `want_amount`.
			let credit_out = match <AssetConversion as SwapCredit<_>>::swap_exact_tokens_for_tokens(
				vec![swap_asset, want_asset_id],
				credit_in,
				Some(want_amount),
			) {
```
