This confirms `Type = AssetConversion` (the real `pallet_asset_conversion` pallet) is wired as `Swap` in the AssetHub Westend runtime, and `RegisterTokenOrigin` allows any local/foreign asset owner (not just Root) — so `register_token` for a non-Root asset owner, and `add_tip` (any `ensure_signed` account) both reach `swap_and_burn` with `None` as the slippage guard. This matches the claim precisely.

Audit Report

## Title
Zero-slippage AMM swap in `swap_and_burn` allows sandwich attack that drains user tip/fee assets and underfunds Ethereum-side execution - (File: bridges/snowbridge/pallets/system-frontend/src/lib.rs)

## Summary
`Pallet::swap_and_burn`, invoked from both `register_token` and `add_tip`, calls `T::Swap::swap_exact_tokens_for_tokens` with `amount_out_min` hardcoded to `None`, disabling slippage protection on the trade converting a user's fee/tip asset into Ether before burning it for teleport. Since `T::Swap = AssetConversion` (the public `pallet_asset_conversion` pallet) in the AssetHub Westend runtime wiring, and both entry points are reachable by ordinary signed accounts (and non-root asset owners for `register_token`), any actor can sandwich the swap and drive the user's `ether_gained` toward zero.

## Finding Description
`swap_and_burn` performs the unprotected swap with an explicit comment confirming intent: [1](#0-0) . This value, `ether_gained`, becomes the `amount` field forwarded verbatim to `EthereumSystemCall::RegisterToken` and `EthereumSystemCall::AddTip` on BridgeHub: [2](#0-1) [3](#0-2) .

`add_tip` is reachable by any `ensure_signed` account with no further origin check: [4](#0-3) . `register_token`'s `RegisterTokenOrigin` in the AssetHub Westend runtime is `EitherOf<EitherOf<LocalAssetOwner, ForeignAssetOwner>, EnsureRootWithSuccess>` — i.e., ordinary local/foreign asset owners (not just Root) can trigger the fee-paying, swap-invoking path: [5](#0-4) . The same config wires `type Swap = AssetConversion`, confirming the swap hits the real, public `pallet_asset_conversion` pool rather than a restricted/oracle-priced venue.

The `Swap` trait explicitly supports an `amount_out_min: Option<Balance>` guard for exactly this purpose [6](#0-5) , and `pallet_asset_conversion`'s own dispatchable enforces it via `ProvidedMinimumNotSufficientForSwap` when a `Some` minimum is supplied [7](#0-6) . `swap_and_burn` is the only call site in the repository that deliberately opts out by passing `None`, unlike the XCM `SingleAssetExchangeAdapter`, which always supplies a real minimum [8](#0-7) .

## Impact Explanation
This is public underpriced work / value-loss on a live-scope Snowbridge pivot: the tip/fee amount forwarded to BridgeHub's `EthereumSystem::register_token`/`add_tip` (the exact corrupted value is `ether_gained`, i.e., the `amount` field in `EthereumSystemCall::RegisterToken`/`AddTip`) can be driven arbitrarily low by a sandwiching attacker while the victim's entire input asset is consumed and burned via `burn_for_teleport`. For `add_tip`, the relayer reward tip recorded for a `message_id` is minted/allocated near zero despite the user paying full price — a direct fund-loss condition in the bridge's reward/delivery accounting. For `register_token`, the reported execution fee sent to Ethereum can be deflated, risking under-funded execution on the Ethereum side while token registration still proceeds.

## Likelihood Explanation
Feasible with no privileged role: any address can observe a pending `register_token`/`add_tip` transaction in the pool and submit a front-run/back-run pair against the same `pallet_asset_conversion` pool leg within the block-building window — a standard MEV/sandwich pattern requiring only capital proportional to pool depth, not validator or relayer collusion.

## Recommendation
Add a real `amount_out_min` parameter to `register_token`/`add_tip` (or derive a conservative bound via `QuotePrice::quote_price_exact_tokens_for_tokens` at call time with a tolerance) and pass it into `T::Swap::swap_exact_tokens_for_tokens` in place of `None`, so the swap and extrinsic revert if pool price has been manipulated beyond an acceptable slippage bound.

## Proof of Concept
1. Attacker observes a pending `add_tip(message_id, asset)` call in the mempool with `asset = (tip_asset_location, 1000)`.
2. Attacker front-runs with a large `tip_asset → ether` swap on the same `pallet_asset_conversion` pool, moving the price against the victim.
3. Victim's `add_tip` executes `swap_and_burn` → `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)`, which succeeds despite a heavily discounted `ether_gained` since no minimum is enforced.
4. Attacker back-runs with the reverse trade, restoring price and capturing the spread as profit.
5. The discounted `ether_gained` is burned via `burn_for_teleport` and forwarded as `amount` in `EthereumSystemCall::AddTip`, so the relayer reward recorded on BridgeHub for `message_id` is far below what the user paid, while the attacker extracted the difference from the pool. A Rust integration test can set up a `pallet_asset_conversion` pool, submit an interleaved front-run/`add_tip`/back-run sequence, and assert `ether_gained` is far below the fair-value quote from `quote_price_exact_tokens_for_tokens` taken before the attack.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L301-308)
```rust
			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs (L50-82)
```rust
	type RegisterTokenOrigin = EitherOf<
		EitherOf<
			LocalAssetOwner<
				AssetIdForTrustBackedAssetsConvert<TrustBackedAssetsPalletLocation, Location>,
				Assets,
				AccountId,
				AssetIdForTrustBackedAssets,
				Location,
			>,
			ForeignAssetOwner<
				(
					FromSiblingParachain<parachain_info::Pallet<Runtime>, Location>,
					xcm_config::bridging::to_rococo::RococoAssetFromAssetHubRococo,
				),
				ForeignAssets,
				AccountId,
				LocationToAccountId,
				Location,
			>,
		>,
		EnsureRootWithSuccess<AccountId, RootLocation>,
	>;
	#[cfg(not(feature = "runtime-benchmarks"))]
	type XcmSender = XcmRouter;
	#[cfg(feature = "runtime-benchmarks")]
	type XcmSender = benchmark_helpers::DoNothingRouter;
	type AssetTransactor = AssetTransactors;
	type EthereumLocation = FeeAsset;
	type XcmExecutor = XcmExecutor<XcmConfig>;
	type BridgeHubLocation = SiblingBridgeHub;
	type UniversalLocation = UniversalLocation;
	type PalletLocation = SystemFrontendPalletLocation;
	type Swap = AssetConversion;
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
