## Analysis

The reported pattern — a swap function that omits the user-supplied minimum-output guard that sibling swap functions in the same codebase do enforce — has a direct analog in `bridges/snowbridge/pallets/system-frontend/src/lib.rs`.

### Title
Snowbridge `swap_and_burn` performs the tip/fee-asset-to-Ether swap with no slippage protection, exposing users to sandwich attacks - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`pallet-asset-conversion` exposes both a `Swap` trait and dispatchable extrinsics (`swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens`) that all correctly honor a caller-supplied `amount_out_min`/`amount_in_max` for slippage protection [1](#0-0) . However, the Snowbridge `snowbridge-pallet-system-frontend`'s internal helper `swap_and_burn` calls `T::Swap::swap_exact_tokens_for_tokens` with the `amount_out_min` argument hardcoded to `None`, explicitly disabling slippage control: [2](#0-1) 

### Finding Description
`swap_and_burn` is reached from two unprivileged, signed-origin extrinsics:
- `register_token`, via `swap_fee_asset_and_burn`, where any user (non-root origin) supplies an arbitrary `fee_asset` [3](#0-2) 
- `add_tip`, where any signed user supplies an arbitrary `asset` to be swapped for Ether and used as a relayer tip [4](#0-3) 

Both paths funnel into `swap_fee_asset_and_burn` → `swap_and_burn`, which swaps the user's `tip_asset_location` for `ether_location` via `T::Swap::swap_exact_tokens_for_tokens(who, swap_path, tip_amount, None, who, true)`. Because `amount_out_min` is `None`, the underlying AMM (`pallet-asset-conversion`) skips the `amount_out >= amount_out_min` check entirely [5](#0-4)  and accepts whatever output the pool state yields at execution time — exactly the missing invariant described in the external JOJO report.

An attacker who observes a pending `register_token`/`add_tip` call in the mempool can sandwich it: front-run to move the pool's `tip_asset_location`/`ether_location` price unfavorably, let the victim's swap execute at the degraded rate (extracting the entire slippage as MEV since there is no floor), then back-run to restore price. The victim still pays the full `tip_amount`/`fee_amount` from their account, but `ether_gained` — the value burned for teleport to Ethereum and used to compute the amount recorded on BridgeHub via `build_register_token_call`/`build_add_tip_call` — can be arbitrarily reduced.

### Impact Explanation
`ether_gained` is not just an internal accounting number: it becomes the `amount` field forwarded in the `RegisterToken`/`AddTip` calls dispatched to BridgeHub's backend pallet [6](#0-5) , which in turn determines the execution fee available on Ethereum for `RegisterToken` or the relayer reward for `AddTip`. A sandwiched, under-delivered `ether_gained` can:
- Cause underpriced/underfunded execution on the Ethereum side of the bridge (fee insufficient to cover Ethereum gas), stalling message processing.
- Silently reduce the tip paid to relayers, degrading bridge liveness incentives.
- Result in direct value loss for the user, who is charged the full `tip_amount`/`fee_amount` of their asset but receives less Ether-equivalent value than the pool price implied moments earlier.

This aligns with the "public underpriced work that degrades... stalls bridge processing" and unbacked value-transfer impact categories.

### Likelihood Explanation
This requires no privileged actor, malicious relayer, or governance action — any ordinary user calling `register_token` (non-root origin) or `add_tip` with a non-Ether fee/tip asset triggers the unguarded swap. Any third party capable of ordering transactions within a block (a standard MEV searcher) can sandwich it. The sibling AMM functions in the same repository already demonstrate the guard exists and is normally enforced, and its explicit `None` here (with the comment `// No minimum amount required`) shows it was deliberately omitted, not merely defaulted.

### Recommendation
Thread a caller-supplied (or governance/config-derived, conservatively-bounded) minimum-Ether-out parameter through `register_token`/`add_tip` → `swap_fee_asset_and_burn` → `swap_and_burn`, and pass `Some(min_ether_out)` to `T::Swap::swap_exact_tokens_for_tokens` instead of `None`, mirroring the slippage protection already enforced by `pallet_asset_conversion::Pallet::swap_exact_tokens_for_tokens` and `do_swap_exact_tokens_for_tokens`.

### Proof of Concept
1. Attacker monitors the transaction pool for a pending `register_token(asset_id, metadata, fee_asset)` or `add_tip(message_id, asset)` call where `fee_asset`/`asset` ≠ Ether location.
2. Attacker front-runs with a large swap in the same pool (`fee_asset_location` ⇄ `ether_location`) to move the price against the victim.
3. Victim's extrinsic executes `swap_and_burn`, calling `T::Swap::swap_exact_tokens_for_tokens(..., tip_amount, None, ...)` [7](#0-6) ; because `amount_out_min` is `None`, `do_swap_exact_tokens_for_tokens` skips the minimum check and returns whatever (degraded) `amount_out` the manipulated pool state produces [1](#0-0) .
4. Attacker back-runs to restore the pool price and pocket the difference.
5. Victim's `ether_gained` (and thus the fee/tip amount recorded on BridgeHub) is lower than the fair-market value of `tip_amount`, while the victim's account was debited the full `tip_amount`.

### Citations

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L296-309)
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

```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L320-351)
```rust
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
