## Title
Missing slippage protection in Snowbridge `system-frontend` tip/fee swap allows extraction of user value via sandwich manipulation - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
The `swap_and_burn` helper in `pallet_system_frontend` (Snowbridge) converts a user-supplied tip/fee asset into Ether by calling `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)`, explicitly passing `None` for `amount_out_min`. This is the exact "no minimum output enforced" pattern described in the external report (`amountOutMinimum = 0`), but here it lives on-chain in a public, unprivileged extrinsic path (`add_tip`, `register_token`) that swaps against `pallet-asset-conversion` pools, exposing users to sandwich/price-manipulation losses.

## Finding Description
`swap_and_burn` builds a swap path and calls the `Swap` trait method with no minimum output: [1](#0-0) 

```
let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
    who.clone(),
    swap_path,
    tip_amount,
    None, // No minimum amount required
    who,
    true,
)?;
```

This is invoked from two unprivileged, publicly callable extrinsics:
- `add_tip`, callable by any `ensure_signed` account, swaps an arbitrary user-supplied `asset` for ether via `swap_fee_asset_and_burn` → `swap_and_burn`, then dispatches an `AddTip` transact call to BridgeHub carrying `ether_gained` as the tip amount: [2](#0-1) 

- `register_token`, open to any origin satisfying `RegisterTokenOrigin` (not restricted to root), similarly swaps the caller's `fee_asset` for ether and uses the resulting `ether_gained` amount to fund the remote `RegisterToken` transact call: [3](#0-2) 

The underlying `pallet_asset_conversion::Swap::swap_exact_tokens_for_tokens` only enforces a minimum output when `amount_out_min` is `Some(_)`; when `None`, `do_swap_exact_tokens_for_tokens` skips the check entirely and accepts whatever the pool state yields: [4](#0-3) 

Because AMM pool pricing (`get_amount_out`) is a function of live reserves, and any account can call `pallet_asset_conversion::swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` against the same pool in the same or an adjacent block, an attacker can push the pool price against the victim immediately before the victim's `add_tip`/`register_token` transaction executes, then reverse the trade afterward, extracting the slippage as arbitrage profit. Since `amount_out_min = None`, the swap inside `swap_and_burn` cannot fail on unfavorable pricing—it simply accepts a minimal `ether_gained`, which is then burned/teleported and used as the cross-chain tip or registration-fee amount. The user's `tip_asset`/`fee_asset` is fully consumed (withdrawn and burned) regardless of how little ether was actually realized, so the corrupted value is `ether_gained` in `swap_and_burn`, which under-values the burned asset without any guard rail.

## Impact Explanation
This directly reproduces the report's bug class ("no minimum output ⇒ market manipulation ⇒ victim receives far less than expected, or effectively nothing") but here it sits in live production Snowbridge code, not a mock or test helper. Impact:
- Users calling `add_tip` or `register_token` can have their fee/tip asset consumed while receiving a manipulated, much lower `ether_gained`, resulting in direct fund loss for the caller and a devalued relayer tip / registration fee actually delivered to BridgeHub/Ethereum.
- This degrades the intended relayer incentive/reward mechanism (tip amounts registered on BH can be silently minimized) and can under-fund `register_token`'s ether-based deposit, causing downstream failures or reduced security margins in the bridge's cross-chain fee accounting.
- The attack requires no admin, governance, relayer, or validator privilege—any account with the ability to trade against the relevant `pallet-asset-conversion` pool can execute it, satisfying the "unprivileged attacker causes fund loss/wrong amount" criterion.

## Likelihood Explanation
Likelihood is Medium: it requires an attacker to identify a pending `add_tip`/`register_token` call with a swappable, thin-liquidity pool and execute standard AMM sandwich mechanics (two of their own transactions bracketing the victim's), which is a common and well-understood technique against any unprotected DEX swap, not a privileged or infrastructure-dependent scenario.

## Recommendation
Do not pass `None` for `amount_out_min` in `swap_and_burn`. Compute an expected output using `AssetConversionApi::quote_price_exact_tokens_for_tokens` (or `Pallet::quote_price_exact_tokens_for_tokens`) and apply a caller-specified or protocol-defined slippage tolerance, propagating `Some(min_out)` into `T::Swap::swap_exact_tokens_for_tokens`. Alternatively, expose `amount_out_min`/`amount_out_min_bps` as a parameter on `add_tip`/`register_token` so callers can bound their own slippage, and fail the extrinsic (reverting the withdrawal) if the swap cannot meet it, mirroring the existing `ProvidedMinimumNotSufficientForSwap` guard already implemented in `pallet-asset-conversion`.

## Proof of Concept
1. Attacker and victim both have accounts on the parachain hosting `pallet_system_frontend` with a live `pallet-asset-conversion` pool between `tip_asset_location` and `ether_location`, funded with modest liquidity.
2. Victim submits `add_tip(message_id, asset)` intending to convert `asset` worth `tip_amount` into ether for a relayer tip.
3. Attacker, observing the pending transaction (or simply acting in the same block via transaction ordering), submits a large `swap_exact_tokens_for_tokens` swapping `tip_asset_location → ether_location` first, depleting the ether side of the pool and worsening the exchange rate.
4. The victim's `add_tip` executes `swap_and_burn` → `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)`, which succeeds against the now-unfavorable pool state at `substrate/frame/asset-conversion/src/lib.rs:987-1004` (no `amount_out_min` check applies), returning a much smaller `ether_gained` than the pre-attack quote would have implied.
5. Attacker immediately reverses their swap (`ether_location → tip_asset_location`), restoring the pool and capturing the price impact as profit.
6. Result: the victim's `asset` is fully withdrawn and burned for teleport, but the tip amount registered on BridgeHub (`EthereumSystemCall::AddTip { amount: ether_gained }`) is minimized, while the attacker profits from the sandwich—demonstrating the same "receive far less than expected" outcome flagged in the external report, now realized against a real on-chain pool rather than a Uniswap router call.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L254-273)
```rust
		/// Add an additional relayer tip for a committed message identified by `message_id`.
		/// The tip asset will be swapped for ether.
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::add_tip()
				.saturating_add(T::BackendWeightInfo::transact_add_tip())
		)]
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L296-316)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L987-1004)
```rust
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
```
