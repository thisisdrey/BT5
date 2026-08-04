Found a solid local analog: an AMM swap performed inside a public, permissionless Snowbridge extrinsic with **no minimum-output/slippage protection at all**, directly mirroring the external report's core defect (a downstream settlement amount computed from an AMM price without a properly enforced minimum).

### Title
Unprotected AMM swap in `snowbridge-pallet-system-frontend::add_tip`/`register_token` allows attacker-controlled underpricing of the ether amount forwarded as relayer reward - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`Pallet::add_tip` and `Pallet::register_token` let any signed user supply an arbitrary fee/tip `Asset`, which the pallet swaps into Ether via `pallet_asset_conversion` through `swap_fee_asset_and_burn` → `swap_and_burn`. That helper calls `T::Swap::swap_exact_tokens_for_tokens` with the `amount_out_min` parameter hard-coded to `None` ("No minimum amount required"), then burns whatever Ether came out and forwards that exact `ether_gained` value to Bridge Hub as the relayer reward/fee amount for that message.

### Finding Description [1](#0-0) 

`swap_and_burn` performs the AMM conversion (asset → Ether) via `pallet_asset_conversion::Swap::swap_exact_tokens_for_tokens`, passing `None` for the minimum-out check: [2](#0-1) 

This return value, `ether_gained`, is used unchecked as the exact amount burned for teleport and then as the exact `amount` field forwarded to the `EthereumSystemCall::AddTip`/`RegisterToken` remote call that determines the relayer reward accrued on Bridge Hub: [3](#0-2) [4](#0-3) 

`add_tip` is reachable by any `ensure_signed` origin, and `register_token` is reachable by any origin satisfying `T::RegisterTokenOrigin` (not root-only in general configurations): [5](#0-4) [3](#0-2) 

Because the underlying `pallet_asset_conversion` pool is a plain constant-product AMM (see `do_add_liquidity`/`quote`), its price can be moved arbitrarily by any account with enough of either pooled asset, and the swap enforces slippage only when a caller-supplied minimum is present: [6](#0-5) 

The `None` passed by `swap_and_burn` disables that guard entirely, so the resulting `ether_gained`/reward amount can be pushed arbitrarily close to zero (or, in the reverse direction, temporarily inflated) purely by manipulating the pool's reserves within the caller's own transaction/batch, with no admin, relayer, or validator involvement required.

### Impact Explanation
The value forwarded to Bridge Hub as the relayer reward for delivering the message is derived from an unguarded AMM quote. An unprivileged caller can, in the same batched transaction, swing the `tip_asset/Ether` pool price before calling `add_tip`/`register_token`, causing the computed reward credited on Bridge Hub to be far lower than the tip amount the caller actually paid in the source asset. Since "an upfront fee must be paid for delivering a message" and this fee/reward is what motivates relayers to submit delivery transactions (per the outbound-queue's own fee-model documentation), systematically underpriced rewards degrade or stall processing of the affected outbound message — matching the "public underpriced work that degrades block production or stalls bridge processing" impact category. It also means the amount recorded on-chain does not match the value actually extracted from the user, breaking the intended value-conservation between what is withdrawn/burned and what is credited downstream.

### Likelihood Explanation
No privileged actor, malicious relayer, validator, or off-chain party is required. Any signed account can:
1. Fund/skew the `tip_asset`/Ether pool (e.g., via `pallet_asset_conversion::swap_exact_tokens_for_tokens` or `add_liquidity`/`remove_liquidity`) and
2. Call `add_tip` (or `register_token`) atomically afterward (e.g., via `pallet-utility::batch_all`),

entirely within their own transaction, using only the public dispatchables already exposed by these two pallets. This requires no front-running of another party's transaction — the attacker manipulates and consumes the same pool themselves in one call, so it is not excluded as a "front-run-only" scenario.

### Recommendation
Require callers of `add_tip`/`register_token` to supply an explicit minimum-Ether-out (or minimum-tip-asset-out) parameter, and thread it through to `T::Swap::swap_exact_tokens_for_tokens` instead of hard-coding `None` in `swap_and_burn`. Alternatively, compute an expected output from a governance/oracle-configured reference rate (similar to `PricingParameters::exchange_rate`) and reject swaps that deviate beyond a configured tolerance, so the reward value credited on Bridge Hub cannot be manipulated independently of the value actually withdrawn from the user.

### Proof of Concept
1. Attacker (also acting as an LP) creates/holds a `pallet_asset_conversion` pool between `tip_asset` and `EthereumLocation` (WETH), with modest liquidity.
2. In a single `pallet-utility::batch_all` extrinsic, attacker calls:
   a. `AssetConversion::swap_exact_tokens_for_tokens` (or `remove_liquidity`) to drain most of the Ether-side reserve of the pool, temporarily crashing the `tip_asset → Ether` price.
   b. `SystemFrontend::add_tip(message_id, Asset { id: tip_asset, fun: Fungible(large_amount) })`, which internally calls `swap_and_burn` with `amount_out_min = None` at `bridges/snowbridge/pallets/system-frontend/src/lib.rs:301-308`; because the pool is drained, `ether_gained` returned is near zero despite `large_amount` of `tip_asset` being withdrawn and swapped.
   c. Restore the pool (e.g., `add_liquidity` back or reverse swap) within the same batch to normalize price for further reuse.
3. The `AddTip` XCM `Transact` sent to Bridge Hub (built in `build_add_tip_call`, `lib.rs:340-351`) carries the near-zero `amount`, so the relayer reward credited for `message_id` on Bridge Hub is far below what the attacker's `tip_asset` was actually worth, while no minimum-output check ever fires to abort the operation.

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
