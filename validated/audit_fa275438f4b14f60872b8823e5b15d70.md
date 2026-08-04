## Title
Missing slippage protection in Snowbridge `system-frontend` fee-swap lets any user's tip/registration payment be drained via price manipulation - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The Snowbridge `pallet-system-frontend` exposes two public, unprivileged extrinsics — `register_token` and `add_tip` — that internally swap a user-supplied fee/tip asset into Ether through `pallet_asset_conversion`'s AMM before burning the Ether for teleport to Ethereum. Unlike the pallet's own public `swap_exact_tokens_for_tokens` extrinsic, which enforces a caller-supplied `amount_out_min`, the internal call made by `system-frontend` passes `None`, completely disabling slippage protection.

### Finding Description
`Pallet::add_tip` and `Pallet::register_token` both route through `swap_fee_asset_and_burn` → `swap_and_burn`, which calls: [1](#0-0) 

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

`T::Swap` is `pallet_asset_conversion::Swap`, whose own dispatchable path deliberately requires an `amount_out_min` and rejects the trade with `ProvidedMinimumNotSufficientForSwap` if the AMM price is unfavorable: [2](#0-1) 

The pallet's design explicitly supports and expects callers to set a minimum bound (see the doc comment "Use `amount_out_min` to control slippage" on `quote_price_exact_tokens_for_tokens`). `system-frontend`, however, hard-codes `None`, so `do_swap_exact_tokens_for_tokens` skips the `amount_out >= amount_out_min` check entirely and accepts whatever output the constant-product pool yields at that moment: [3](#0-2) 

`add_tip` is callable by any `ensure_signed` account with an arbitrary `Asset` and amount, and `register_token` is reachable by any non-root origin: [4](#0-3) [5](#0-4) 

Because the pool used is the same on-chain, publicly tradable `pallet_asset_conversion` pool (frequently thin ICHI-style pairs for niche fee assets), the resulting `ether_gained` value can be pushed arbitrarily low by anyone who trades against the pool immediately before the victim's `add_tip`/`register_token` extrinsic executes, and pushed back afterward. The full `tip_amount`/fee asset is still withdrawn and burned from the caller (`burn_for_teleport`), but the `ether_gained`/`amount` value forwarded on-chain to BridgeHub (used to size the relayer reward or registration fee) can be reduced to near zero.

### Impact Explanation
This directly hits two impact categories in scope:
- **Bridge reward/payout integrity**: `add_tip`'s whole purpose is to fund a relayer reward keyed by `ether_gained`. If that value can be forced near zero while the user's tip asset is fully consumed, the tip becomes economically worthless, degrading/stalling Snowbridge outbound message processing (underpriced work with bridge-processing impact) while the paying account's funds are irrecoverably spent.
- **Fund loss for the caller**: the user pays the full `fee_asset`/`tip_amount`, which is withdrawn and burned regardless of how bad the swap execution was, so value is destroyed without the intended benefit reaching the beneficiary (relayer reward pool / registration fee on Ethereum side).

This differs from the excluded "front-run-only" class in that the root cause is a code defect: the pallet already has, and enforces elsewhere, a slippage-bound mechanism (`amount_out_min` / `ProvidedMinimumNotSufficientForSwap`) that `system-frontend` simply opts out of by passing `None`, unlike every other public swap surface in the codebase (direct `swap_exact_tokens_for_tokens` extrinsic, `SwapFirstAssetTrader` mock, XCM `SingleAssetExchangeAdapter`, all of which thread a real minimum through).

### Likelihood Explanation
Any unprivileged, signed account can trigger `add_tip` with an arbitrary asset/amount, and the AMM pools it swaps against are themselves publicly tradable via `AssetConversion::swap_exact_tokens_for_tokens`/`add_liquidity`. No governance, admin, relayer, or validator privilege is required to trade against the same pool in adjacent transactions — this is ordinary public AMM interaction, and the vulnerability is structural (missing parameter), not merely opportunistic.

### Recommendation
Add an explicit, user-supplied (or oracle-derived) minimum output parameter to `add_tip`/`register_token`, and thread it through `swap_and_burn`/`swap_fee_asset_and_burn` into `T::Swap::swap_exact_tokens_for_tokens` instead of hard-coding `None`, mirroring the protection already implemented in `pallet_asset_conversion::Pallet::swap_exact_tokens_for_tokens`.

### Proof of Concept
1. Identify a `system-frontend` deployment where `T::Swap` points at a `pallet_asset_conversion` pool for a low-liquidity fee asset ↔ Ether pair.
2. Attacker submits a large `swap_exact_tokens_for_tokens` (or `add_liquidity`/`remove_liquidity` sequence) against that pool to push the fee-asset price down immediately before the victim's transaction is included.
3. Victim calls `add_tip(message_id, asset)` with a normal tip amount; `swap_and_burn` executes `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)`, accepting whatever `ether_gained` the manipulated pool returns — verified at [1](#0-0)  since no `ensure!(amount_out >= amount_out_min)` check exists on this path unlike [6](#0-5) .
4. Attacker reverses the initial trade, pocketing the price difference; the victim's tip asset is fully burned for a negligible `ether_gained`, which is then embedded in the `AddTip`/`RegisterToken` transact call sent to BridgeHub.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L988-1004)
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

			Self::swap(&sender, &path, &send_to, keep_alive)?;
```
