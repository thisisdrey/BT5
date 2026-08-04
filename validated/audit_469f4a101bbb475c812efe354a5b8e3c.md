### Title
Missing slippage protection in Snowbridge `system-frontend` fee/tip swap enables sandwich extraction of bridge fees and relayer tips - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The `pallet-snowbridge-system-frontend`'s internal `swap_and_burn` helper — invoked by the public extrinsics `register_token` and `add_tip` — converts a user-supplied fee/tip asset into Ether via `pallet_asset_conversion::Swap::swap_exact_tokens_for_tokens`, but hardcodes `amount_out_min` to `None`. This mirrors exactly the reported bug class: an AMM swap performed with zero slippage protection, letting a searcher sandwich the swap and skim value that should have gone toward the bridge fee/reward.

### Finding Description
`swap_and_burn` builds the swap path and calls the configured `Swap` implementation with no minimum-out guard: [1](#0-0) 

Concretely:
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

This is reached from two public, unprivileged dispatchables:
- `register_token`, where any origin can supply an arbitrary `fee_asset` that is swapped for Ether via `swap_fee_asset_and_burn` and the resulting `ether_gained` is forwarded as the `amount` paid for remote `RegisterToken` execution on Ethereum. [2](#0-1) 
- `add_tip`, where any signed account can add a relayer tip by supplying an asset that gets swapped to Ether and the swap output becomes the tip amount forwarded to `EthereumSystem::AddTip` on Bridge Hub. [3](#0-2) 

`T::Swap` is bound to the real on-chain `pallet_asset_conversion::Pallet<Runtime>` (a Uniswap-v2-style constant-product AMM) as configured for Asset Hub Westend, so the swap price is subject to normal AMM slippage and is manipulable within the same block by front-running/back-running the swap (classic sandwich). Unlike the underlying `pallet_asset_conversion::do_swap_exact_tokens_for_tokens`, which does support and enforce an `amount_out_min` check (`ensure!(amount_out >= amount_out_min, Error::<T>::ProvidedMinimumNotSufficientForSwap)`), the system-frontend pallet deliberately discards this protection by always passing `None`: [4](#0-3) 

No other guard exists in `swap_fee_asset_and_burn` / `swap_and_burn` to bound the acceptable output, and the caller (the extrinsic submitter) has no way to specify a minimum acceptable `ether_gained`.

### Impact Explanation
Because the derived `ether_gained` directly determines (a) the fee amount attached to the remote `RegisterToken` transact call, and (b) the relayer tip amount registered via `AddTip` on Bridge Hub, an attacker who sandwiches the swap can force `ether_gained` down to a fraction of its fair value. This can:
- Cause `register_token` to submit an underfunded remote execution amount, risking failure or under-provisioning of the Ethereum-side registration (public underpriced work degrading bridge processing).
- Let an attacker siphon value out of the pool at the tipper's expense, silently shrinking relayer rewards/tips below what the user intended, without any error or revert to signal the loss.

This is a value-conservation violation on a public, unprivileged, fee/reward-relevant code path in the Snowbridge BridgeHub/AssetHub flow, fitting the "public underpriced work that degrades... stalls bridge processing" and "theft or unbacked... duplicate settlement" impact categories via silent value extraction from the swap.

### Likelihood Explanation
`register_token` and `add_tip` are both open to any signed/XCM origin with no special privilege required; the attacker only needs to observe the pending extrinsic (mempool/block-building visibility) and place trades before/after it in the AMM pool used for `T::Swap`. No malicious validator, relayer, or governance actor is needed — this is a pure sandwich against a public, permissionless entry point, satisfying the "unprivileged attacker" and "no malicious peer/validator/relayer" constraints in the task.

### Recommendation
Thread a caller-specified `amount_out_min` (or a computed minimum derived from a recent on-chain quote with a bounded tolerance) through `register_token`/`add_tip` down into `swap_and_burn`'s call to `T::Swap::swap_exact_tokens_for_tokens`, replacing the hardcoded `None`. Reject the extrinsic (propagating `Error::<T>::ProvidedMinimumNotSufficientForSwap`-equivalent) if the swap cannot achieve the minimum, consistent with how `pallet_asset_conversion` itself protects direct swap callers.

### Proof of Concept
1. Attacker monitors the mempool/next block for a `register_token` or `add_tip` call with a sizeable `fee_asset`/tip asset amount.
2. Attacker front-runs with a large swap in the same AMM pool (`fee_asset` → Ether) to push the price against the pending call, then the victim's `swap_and_burn` executes at the worsened price with `amount_out_min = None` so it cannot revert.
3. Attacker back-runs, reversing their trade and capturing the difference, while `ether_gained` used for `RegisterToken`/`AddTip` is now far below fair value — reflected directly in the code path at: [5](#0-4) 
4. The under-valued `ether_gained` is then forwarded as the `amount`/tip to Bridge Hub via `build_register_token_call`/`build_add_tip_call`, causing under-priced remote execution or a diminished relayer reward with no on-chain indication of the loss.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L296-317)
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
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L987-1002)
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
```
