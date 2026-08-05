The strongest local analog to the SEuroOffering "missing minimum output" bug is in the Snowbridge system-frontend pallet's swap-and-burn helper, which — unlike every other swap path in this codebase — deliberately omits slippage protection.

### Title
Missing minimum-output protection in Snowbridge `swap_and_burn` lets tip/fee swaps be sandwiched, silently underpaying relayer rewards and burning less ether than intended - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`pallet-asset-conversion` and every consumer of its `Swap`/`SwapCredit` traits in this repository (extrinsics, precompiles, the XCM `SingleAssetExchangeAdapter`) correctly thread an `amount_out_min`/`want_amount` bound through to `do_swap_exact_tokens_for_tokens`, which enforces `ProvidedMinimumNotSufficientForSwap` before executing the swap. The Snowbridge `pallet-snowbridge-system-frontend`, however, calls `T::Swap::swap_exact_tokens_for_tokens` with an explicit `None` for the minimum-output parameter in its internal `swap_and_burn` helper. This helper is reachable from two public, unprivileged extrinsics — `add_tip` and `register_token` — turning it into the same "no min-received guard" pattern flagged in the SEuroOffering report, but here it degrades the DOT-side relayer reward for Ethereum message delivery.

### Finding Description
`swap_and_burn` builds the swap path and calls the trait method with no floor: [1](#0-0) 

This is invoked from `swap_fee_asset_and_burn`, which is called by:
- `add_tip(origin, message_id, asset)` — any signed account can call this to add a relayer tip for an already-committed message, using an arbitrary fungible `asset` as the tip currency: [2](#0-1) 
- `register_token(origin, asset_id, metadata, fee_asset)` — reachable by any origin satisfying `RegisterTokenOrigin` (not root-only, since the `is_here()` branch is the privileged exception): [3](#0-2) 

Contrast this with the canonical, guarded implementation of the same swap in `pallet-asset-conversion`, which requires and checks a minimum: [4](#0-3) 

and with the dedicated regression test proving the pallet enforces this bound when a caller supplies it: [5](#0-4) 

The `ether_gained` value returned by the unbounded swap is used verbatim as the on-chain relayer reward/tip amount forwarded to BridgeHub, and as the teleported ether amount for `register_token`: [6](#0-5) [7](#0-6) 

### Impact Explanation
Because no `amount_out_min` is enforced, an attacker who can influence the `fee_asset`/ether AMM pool price within the same block (e.g., by front/back-running the `add_tip` or `register_token` extrinsic through other public swap or liquidity calls in `pallet-asset-conversion`) can push the realized `ether_gained` far below the fair-value quote for `tip_amount`. Consequences:
- For `add_tip`: the user's tip asset is fully consumed, but the ether amount credited as relayer reward on BridgeHub can be minimized, effectively degrading/underpricing the public relayer-reward mechanism that is supposed to incentivize timely bridge message delivery — directly matching the "public underpriced work that degrades... stalls bridge processing" impact category.
- For `register_token`: the `amount` passed to `EthereumSystem::RegisterToken` (used for gas/ether funding of the ERC20 deployment on Ethereum) can likewise be minimized, potentially causing the remote registration call to fail or be underfunded despite the user paying full price in `fee_asset`.
- In both cases the user has no way to bound their loss; the swap always succeeds at whatever price is available, silently transferring value to whoever manipulated the pool.

### Likelihood Explanation
Both `add_tip` and `register_token` are unprivileged, signed-origin extrinsics that anyone can call with an attacker-influenceable `asset`/`fee_asset`. The asset-conversion pools that back these swaps are themselves permissionless (anyone can add/remove liquidity or execute swaps), so manipulating short-term pricing around a target transaction requires no privileged access, validator collusion, or off-chain infrastructure — only ordinary extrinsic submission, which is squarely in scope per the "public underpriced work" and unauthorized value-extraction pivots.

### Recommendation
Thread a caller- or config-supplied minimum-output bound through `swap_and_burn`/`swap_fee_asset_and_burn` instead of hardcoding `None`, mirroring the `amount_out_min` pattern already used by `pallet-asset-conversion`'s public extrinsics. At minimum, add an `amount_out_min: u128` parameter to `add_tip` and `register_token`, pass `Some(amount_out_min)` into `swap_exact_tokens_for_tokens`, and propagate the pallet's existing `Error::SwapError` on failure so users can bound acceptable slippage the same way `ProvidedMinimumNotSufficientForSwap` protects other swap paths.

### Proof of Concept
1. Attacker observes a pending `add_tip(message_id, asset=(TOKEN_X, tip_amount))` transaction in the pool (or simply calls it themselves in the same block as their own trades).
2. Attacker swaps a large amount of ether (or the counter asset) into the `TOKEN_X`/`ETHER` pool via `AssetConversion::swap_exact_tokens_for_tokens` immediately before the victim's `add_tip` call, moving the pool price so `TOKEN_X` is temporarily cheap relative to ether.
3. Victim's `add_tip` executes `swap_and_burn` with `amount_out_min = None`; the swap succeeds at the manipulated price, producing a much smaller `ether_gained` than the fair-value quote.
4. Attacker immediately reverses their trade in a following transaction to restore the pool price and recoup their capital, keeping the difference extracted from the victim's tip.
5. The relayer reward recorded on BridgeHub for `message_id` is now far lower than the victim intended, while their full `tip_amount` of `TOKEN_X` was consumed — reproducible deterministically given `pallet-asset-conversion`'s constant-product formula and the absence of any `ensure!` on `ether_gained` in `swap_and_burn`.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L310-317)
```rust
			// Burn the ether
			let ether_asset = Asset::from((ether_location.clone(), ether_gained));

			burn_for_teleport::<T::AssetTransactor>(&origin, &ether_asset)
				.map_err(|_| Error::<T>::BurnError)?;

			Ok(ether_gained)
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

**File:** substrate/frame/asset-conversion/src/tests.rs (L1600-1613)
```rust
		let exchange_amount = 100;

		assert_noop!(
			AssetConversion::swap_exact_tokens_for_tokens(
				RuntimeOrigin::signed(user),
				bvec![token_2.clone(), token_1.clone()],
				exchange_amount, // amount_in
				4000,            // amount_out_min
				user,
				false,
			),
			Error::<Test>::ProvidedMinimumNotSufficientForSwap
		);
	});
```
