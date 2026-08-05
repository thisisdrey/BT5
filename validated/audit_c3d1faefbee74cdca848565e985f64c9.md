Audit Report

## Title
Unbounded-slippage swap in Snowbridge `system-frontend` fee/tip conversion allows sandwich-based theft of user tip/fee assets - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`Pallet::swap_and_burn` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` converts a user-supplied tip/fee asset into Ether by calling `T::Swap::swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None`, unconditionally disabling the slippage-protection mechanism that `pallet_asset_conversion` explicitly exposes for this purpose. Both `add_tip` (callable by any signed account) and `register_token` (for non-root origins) route through this unguarded swap, so an attacker can manipulate the underlying `pallet_asset_conversion` pool reserves via ordinary public swap/liquidity calls immediately before a victim's call executes, causing the victim's tip/fee asset to be converted to far less Ether than fair value.

## Finding Description
`swap_and_burn` builds a two-hop swap path `[tip_asset_location, ether_location]` and calls: [1](#0-0) 
passing `None` explicitly for `amount_out_min`, with the code comment "No minimum amount required". This function is reached from two public, non-privileged entrypoints:
- `add_tip`, callable by any signed account, which immediately calls `swap_fee_asset_and_burn` → `swap_and_burn`: [2](#0-1) 
- `register_token`, for any non-root origin whose asset is nested within their own consensus system, which also calls `swap_fee_asset_and_burn`: [3](#0-2) 

Both converge on the shared helper: [4](#0-3) 

The `Swap<AccountId>` trait explicitly supports an `Option<Balance>` minimum specifically to guard against unfavorable execution: [5](#0-4) 

and `pallet_asset_conversion`'s underlying dispatch path enforces `ProvidedMinimumNotSufficientForSwap` only when a `Some(min)` is actually provided: [6](#0-5) 

Because `system-frontend` always passes `None`, this check is unconditionally skipped for every `add_tip`/`register_token` invocation that requires a swap (i.e., whenever the fee/tip asset is not already Ether), regardless of how thin or freshly manipulated the pool is. There is no caller-supplied slippage parameter and no on-chain fallback (e.g., via `QuotePrice`) computed before the swap to bound the trade.

## Impact Explanation
The resulting `ether_gained` value is used directly as the `amount` field dispatched to BridgeHub in both flows: [7](#0-6) [8](#0-7) 

This `amount` becomes the basis for the relayer reward (`add_tip`) or registration fee funding (`register_token`) on BridgeHub. An attacker can sandwich a victim's `add_tip`/`register_token` call by manipulating the relevant `pallet_asset_conversion` pool (via ordinary, permissionless `swap_exact_tokens_for_tokens`/liquidity calls) immediately beforehand, causing the victim's tip/fee asset to be converted at a degraded price. This directly corrupts the `ether_gained`/`amount` value credited toward the relayer reward or fee-funding basis, extracting value from the victim and degrading the fidelity of Snowbridge's tip/reward accounting — aligned with "public underpriced work" and "theft" impact categories in scope.

## Likelihood Explanation
No privileged role, governance action, or compromised relayer/validator is required. Any account can observe a pending `add_tip`/`register_token` extrinsic in the mempool and front-run/sandwich it using the fully public `pallet_asset_conversion` extrinsics (swap or remove liquidity) against the exact pool pair used in the swap path. Since the `None` is hard-coded in pallet source rather than caller-supplied, every non-Ether-denominated `add_tip`/`register_token` call is unconditionally exposed — this is not merely a front-running opportunity but a structural absence of a slippage floor that the underlying pallet was designed to support.

## Recommendation
Compute a reasonable `amount_out_min` before swapping (e.g., via `QuotePrice::quote_price_exact_tokens_for_tokens`, as already used elsewhere in the codebase such as `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`) with an acceptable slippage tolerance, and pass `Some(min_out)` into `swap_exact_tokens_for_tokens` instead of `None`. Alternatively, add a caller-supplied `min_ether_out`/slippage-tolerance parameter to `add_tip`/`register_token` so users can bound their own risk.

## Proof of Concept
1. Attacker monitors the parachain mempool hosting the `tip_asset_location`/`ether_location` `pallet_asset_conversion` pool for a pending `add_tip(message_id, asset)` or `register_token(...)` extrinsic.
2. Attacker submits a large `swap_exact_tokens_for_tokens` (or `remove_liquidity`) call against the same pool immediately before the victim's extrinsic, using the pallet's own public dispatchables — no special privilege required.
3. The victim's transaction executes `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:301-308`), filling at the manipulated price since no minimum-output guard exists to abort the swap.
4. Attacker reverses the position after the victim's transaction (re-adds liquidity / swaps back), extracting the price differential that was effectively taken from the victim's `ether_gained` value, which propagates into the BridgeHub-side reward/fee `amount`.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L319-338)
```rust
		// Build the call to dispatch the `EthereumSystem::register_token` extrinsic on BH
		fn build_register_token_call(
			sender: Location,
			asset: Location,
			metadata: AssetMetadata,
			amount: u128,
		) -> Result<BridgeHubRuntime<T>, Error<T>> {
			// reanchor locations relative to BH
			let sender = Self::reanchored(sender)?;
			let asset = Self::reanchored(asset)?;

			let call = BridgeHubRuntime::EthereumSystem(EthereumSystemCall::RegisterToken {
				sender: Box::new(VersionedLocation::from(sender)),
				asset_id: Box::new(VersionedLocation::from(asset)),
				metadata,
				amount,
			});

			Ok(call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L340-351)
```rust
		// Build the call to dispatch the `EthereumSystem::add_tip` extrinsic on BH
		fn build_add_tip_call(
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> BridgeHubRuntime<T> {
			BridgeHubRuntime::EthereumSystem(EthereumSystemCall::AddTip {
				sender,
				message_id,
				amount,
			})
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L372-404)
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
		}
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L33-50)
```rust
	/// Swap exactly `amount_in` of asset `path[0]` for asset `path[last]`.
	/// If an `amount_out_min` is specified, it will return an error if it is unable to acquire
	/// the amount desired.
	///
	/// Withdraws the `path[0]` asset from `sender`, deposits the `path[last]` asset to `send_to`,
	/// respecting `keep_alive`.
	///
	/// If successful, returns the amount of `path[last]` acquired for the `amount_in`.
	///
	/// This operation is expected to be atomic.
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
