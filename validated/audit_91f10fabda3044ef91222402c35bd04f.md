### Title
`add_tip` swaps fee assets to Ether with no slippage protection, allowing self-sandwich extraction of AMM liquidity that inflates the burned/teleported Ether amount reported to Bridge Hub - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The Snowbridge `system-frontend` pallet exposes an unprivileged, signed extrinsic `add_tip` that lets any user boost a relayer reward for a pending Ethereum message by swapping an arbitrary local asset for Ether via `pallet_asset_conversion`, then burning that Ether for teleportation and reporting the resulting amount to Bridge Hub as the tip. The swap is executed through the internal `swap_and_burn` helper, which builds a hardcoded, single-hop `path = [tip_asset, ether]` and passes `amount_out_min: None` — i.e., zero minimum-output/slippage protection, mirroring the external report's "hardcoded/naive swap path" defect but with an even stronger loosening (no floor at all). [1](#0-0) 

### Finding Description
`add_tip` is callable by any signed origin and forwards straight into `swap_fee_asset_and_burn` → `swap_and_burn`: [2](#0-1) 

`swap_and_burn` constructs the swap path and calls `T::Swap::swap_exact_tokens_for_tokens` with `None` for `amount_out_min`:
```rust
let swap_path = vec![tip_asset_location.clone(), ether_location.clone()];
...
let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
    who.clone(),
    swap_path,
    tip_amount,
    None, // No minimum amount required
    who,
    true,
)?;
``` [3](#0-2) 

The `Swap` trait implementation used in production is `pallet_asset_conversion`'s `do_swap_exact_tokens_for_tokens`, which computes output strictly from the on-chain pool reserves for that exact `path`, with no protection beyond the caller-supplied `amount_out_min`: [4](#0-3) 

Because `amount_out_min` is hardcoded to `None`, the swap always executes at whatever price the pool currently reflects, with no floor. This is precisely the "hardcoded/no-optimal-path, unguarded swap" pattern from the external report, except worse: the original report's contract at least applied a `minOut` at the outer level; here even that is stripped. An attacker who controls (or trades against) the `tip_asset`/Ether pool can:
1. Submit an initial extrinsic in the same block (or sequence of blocks with predictable ordering) that pushes the pool price of Ether artificially high relative to `tip_asset` (self-sandwich / temporary liquidity skew).
2. Call `add_tip` with a small `tip_amount`, receiving an inflated `ether_gained` from the distorted pool because there is no `amount_out_min` guard to reject the bad rate.
3. Reverse the initial price-skewing trade to recover most of the principal, keeping the inflated `ether_gained`.

The resulting `ether_gained` is what actually gets burned via `burn_for_teleport` and is the exact value forwarded to Bridge Hub as the `AddTip { amount: ether_gained, .. }` command that becomes part of the relayer reward pot on the remote/Ethereum side: [5](#0-4) [6](#0-5) 

Because the burn amount is derived entirely from the manipulable swap output rather than a value independently verified against the actual, unmanipulated market price, an attacker can convert a small amount of `tip_asset` plus temporary liquidity manipulation into a disproportionately large "Ether burned for teleport" credit — extracting value from AMM liquidity providers while inflating the bridge-side reward/teleport accounting.

### Impact Explanation
This falls under "theft or unbacked mint" and "duplicate/incorrect settlement" impact classes: the amount recorded as burned-for-bridging Ether (which underlies real economic value transferred/credited cross-chain as a relayer reward) does not have to reflect real market value, only a manipulable AMM quote with zero slippage protection. This can drain value from the `pallet_asset_conversion` pool (harming LPs) while crediting the attacker (or an accomplice relayer) with an inflated bridge reward, undermining the "conserve value / settle exactly once to rightful beneficiary and amount" invariant for bridge rewards.

### Likelihood Explanation
The attack requires no privileged, governance, validator, relayer, or node-level access — only an ordinary signed account able to (a) call `add_tip` and (b) place trades against the same asset-conversion pool used for the `tip_asset`/Ether pair, which is entirely within reach of any unprivileged user, especially for lower-liquidity registered tokens where a single account may realistically move the price. The `amount_out_min: None` hardcoding removes the one guard (`ProvidedMinimumNotSufficientForSwap`) that `pallet_asset_conversion` normally offers.

### Recommendation
Do not hardcode `amount_out_min` to `None` in `swap_and_burn`. Either:
- Require callers of `add_tip`/`register_token` to supply an `amount_out_min`/max-slippage parameter that is enforced, or
- Compute an expected output using `AssetConversionApi::quote_price_exact_tokens_for_tokens` (or an oracle/TWAP) and reject swaps that deviate materially from that quote, or
- Cap/verify the resulting `ether_gained` against an independent price reference before it is burned and forwarded as the `AddTip`/reward amount to Bridge Hub.

### Proof of Concept
1. Attacker creates or uses an existing `pallet_asset_conversion` pool for `(tip_asset, Ether-representation-asset)` with modest liquidity.
2. In block N, attacker submits a large trade that shifts the pool reserves so that `tip_asset → Ether` temporarily quotes far above fair value.
3. Still in block N (or shortly after), attacker calls `EthereumSystemFrontend::add_tip(message_id, Asset { id: tip_asset_location, fun: Fungible(small_amount) })`.
4. Internally, `swap_and_burn` executes `swap_exact_tokens_for_tokens(..., amount_out_min: None, ...)` against the skewed pool, returning an inflated `ether_gained`, confirmed by the code path at: [7](#0-6) 
5. Attacker reverses the initial skewing trade to restore the pool and recover most of the principal spent in step 2.
6. The inflated `ether_gained` is burned via `burn_for_teleport` and forwarded in `EthereumSystemCall::AddTip { amount: ether_gained, .. }` to Bridge Hub, crediting a reward disproportionate to the attacker's real economic outlay, at the expense of the pool's liquidity providers. [2](#0-1)

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
