Audit Report

## Title
`add_tip` swaps fee assets to Ether with no slippage protection, allowing self-sandwich extraction of AMM liquidity that inflates the burned/teleported Ether amount reported to Bridge Hub - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
The unprivileged, signed extrinsic `add_tip` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` lets any account convert an arbitrary registered asset into Ether via `pallet_asset_conversion` and burn the result for teleportation as a relayer-reward tip forwarded to BridgeHub. The internal `swap_and_burn` helper hardcodes `amount_out_min: None` when calling `T::Swap::swap_exact_tokens_for_tokens`, removing all slippage protection and allowing an attacker who manipulates the underlying AMM pool to extract inflated Ether output that is then burned and credited as an inflated tip.

## Finding Description
`add_tip` (`lib.rs` L261-273) is callable by any `ensure_signed` origin and forwards directly to `swap_fee_asset_and_burn` (L372-404), which calls `swap_and_burn` (L290-317) when the tip asset differs from Ether:

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

`T::Swap` in production is backed by `pallet_asset_conversion::do_swap_exact_tokens_for_tokens` (`substrate/frame/asset-conversion/src/lib.rs` L980-1014), which only enforces `amount_out >= amount_out_min` when `amount_out_min` is `Some`. Passing `None` disables the only guard the pallet offers against unfavorable pricing, and the output is computed purely from current pool reserves for the exact `path = [tip_asset, ether]` with no TWAP or independent price reference.

Pool creation and liquidity provision in `pallet_asset_conversion` on AssetHub is permissionless, so an attacker can create or trade against a low-liquidity `(tip_asset, Ether)` pool, skew its price with a preceding trade, call `add_tip` to receive an inflated `ether_gained` at the manipulated rate (with zero floor rejecting the bad quote), then reverse the skewing trade to recover principal. The resulting `ether_gained` is real — it is actually withdrawn from the pool and burned via `burn_for_teleport` — and is exactly the value placed into `EthereumSystemCall::AddTip { amount: ether_gained, .. }` (L340-351) forwarded to BridgeHub, which credits it toward the relayer reward pot for the referenced message.

The existing checks (`ProvidedMinimumNotSufficientForSwap`) are structurally bypassed because the caller (the pallet, not the end user) never supplies a floor, not because of any bug in `pallet_asset_conversion` itself — the vulnerability is entirely in `system-frontend`'s use of the swap API.

## Impact Explanation
This enables real value extraction from AMM liquidity providers: the attacker profits at the pool's expense while the excess Ether is burned and forwarded as an inflated tip amount to BridgeHub, corrupting the "amount" field settled into the relayer reward accounting without it reflecting genuine market value paid by the tipper. This aligns with the "theft" and "duplicate/incorrect settlement" impact classes for bridge reward correctness, since the amount credited toward relayer rewards is not backed by fair-value economic input from the caller.

## Likelihood Explanation
The attack requires only an ordinary signed account with the ability to call `add_tip` and to trade against the same `pallet_asset_conversion` pool used for the `tip_asset`/Ether pair — both of which are available to any unprivileged user, and pool creation/liquidity provision on AssetHub is itself permissionless, making feasibility highest for newly registered or thin-liquidity tokens where a single account can materially move the price within one or a few blocks.

## Recommendation
Do not hardcode `amount_out_min` to `None` in `swap_and_burn`. Require callers to supply an enforced minimum-output/slippage bound, or compute an expected output via `AssetConversionApi::quote_price_exact_tokens_for_tokens` (or another independent price reference) and reject swaps whose realized output deviates materially from that quote before burning and forwarding the amount to BridgeHub.

## Proof of Concept
1. Attacker creates/uses a `pallet_asset_conversion` pool for `(tip_asset, Ether)` with modest liquidity.
2. In block N, attacker trades to skew the pool so `tip_asset → Ether` quotes above fair value.
3. Attacker calls `EthereumSystemFrontend::add_tip(message_id, Asset { id: tip_asset_location, fun: Fungible(small_amount) })`; internally `swap_and_burn` executes `swap_exact_tokens_for_tokens(..., None, ...)` against the skewed pool and returns an inflated `ether_gained` (`lib.rs` L301-308).
4. Attacker reverses the skewing trade to recover most of the principal.
5. The inflated `ether_gained` is burned via `burn_for_teleport` and forwarded as `EthereumSystemCall::AddTip { amount: ether_gained, .. }` to BridgeHub, crediting a reward disproportionate to the attacker's real economic outlay at the pool LPs' expense. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L980-1014)
```rust
		pub(crate) fn do_swap_exact_tokens_for_tokens(
			sender: T::AccountId,
			path: Vec<T::AssetKind>,
			amount_in: T::Balance,
			amount_out_min: Option<T::Balance>,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> Result<T::Balance, DispatchError> {
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

			Self::swap(&sender, &path, &send_to, keep_alive)?;

			Self::deposit_event(Event::SwapExecuted {
				who: sender,
				send_to,
				amount_in,
				amount_out,
				path,
			});
			Ok(amount_out)
		}
```
