This confirms `Type Swap = AssetConversion` is wired to the real, permissionless `pallet_asset_conversion` in the Asset Hub Westend runtime, and pools can be created/manipulated by any signed account via `create_pool`/`add_liquidity`/`swap_exact_tokens_for_tokens` (as shown in the benchmark helper using a `signed_owner` origin). The `swap_and_burn` function at `bridges/snowbridge/pallets/system-frontend/src/lib.rs:301-308` indeed hard-codes `None` for `amount_out_min`, and this unguarded `ether_gained` flows unchecked into `build_add_tip_call`/`build_register_token_call` as the `amount` field sent to Bridge Hub as the relayer reward.

Audit Report

## Title
Unprotected AMM swap in `snowbridge-pallet-system-frontend::add_tip`/`register_token` allows attacker-controlled underpricing of the ether amount forwarded as relayer reward - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`Pallet::add_tip` and `Pallet::register_token` let any signed/permitted user supply an arbitrary fee/tip `Asset`, which the pallet swaps into Ether via `pallet_asset_conversion` through `swap_fee_asset_and_burn` → `swap_and_burn`. That helper calls `T::Swap::swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None`, then burns whatever Ether came out and forwards that exact `ether_gained` value to Bridge Hub as the relayer reward/fee amount for the message.

## Finding Description
`swap_and_burn` performs the AMM conversion (tip asset → Ether) via `pallet_asset_conversion::Swap::swap_exact_tokens_for_tokens`, passing `None` for the minimum-out check [1](#0-0) . The returned `ether_gained` is used unchecked as the exact amount burned for teleport [2](#0-1)  and then forwarded as the exact `amount` field in the `EthereumSystemCall::AddTip`/`RegisterToken` remote call dispatched to Bridge Hub, which determines the relayer reward accrued there [3](#0-2) [4](#0-3) .

`add_tip` is reachable by any `ensure_signed` origin [5](#0-4) , and `register_token` is reachable by any origin satisfying `T::RegisterTokenOrigin`, which in the Asset Hub Westend runtime configuration includes non-root asset-owner origins (`LocalAssetOwner`/`ForeignAssetOwner`), not only root [6](#0-5) .

Crucially, `T::Swap` is concretely bound to the production `pallet_asset_conversion` instance (`AssetConversion`) in the Asset Hub Westend runtime [7](#0-6) , which is a plain constant-product AMM whose pools any account can create and add liquidity to via public dispatchables (`create_pool`, `add_liquidity`, `swap_exact_tokens_for_tokens`), as also exercised by the benchmark helper using a `signed_owner` (non-privileged) origin [8](#0-7) . The AMM's slippage guard only fires when a caller-supplied `amount_out_min` is `Some(_)` [9](#0-8) ; passing `None` disables this guard entirely.

## Impact Explanation
The value forwarded to Bridge Hub as the relayer reward for delivering the message is derived from an unguarded AMM quote that the caller can move within their own transaction (e.g., via `pallet-utility::batch_all` combining `swap_exact_tokens_for_tokens`/`remove_liquidity` to drain the pool immediately before calling `add_tip`/`register_token`). This lets an unprivileged caller cause the `amount` credited on Bridge Hub as the relayer reward to be far lower than what was actually withdrawn/burned from the caller in the source asset, i.e., named corrupted value: the `amount` field of `EthereumSystemCall::AddTip`/`RegisterToken` (relayer reward payout). This falls under "public underpriced work that degrades block production or stalls bridge processing" since relayers are compensated based on this value and rely on it to justify submitting delivery transactions.

## Likelihood Explanation
No privileged actor, malicious relayer, validator, or off-chain party is required. Any signed account (for `add_tip`) or any account satisfying the non-root asset-owner origin checks (for `register_token`) can atomically manipulate the `tip_asset`/Ether pool reserves and then call `add_tip`/`register_token` in the same batched extrinsic, using only public dispatchables of `pallet_asset_conversion` and `snowbridge-pallet-system-frontend`. This is self-contained (attacker manipulates and consumes their own pool) and repeatable, not a front-run-only scenario.

## Recommendation
Require callers of `add_tip`/`register_token` to supply an explicit minimum-Ether-out parameter and thread it through to `T::Swap::swap_exact_tokens_for_tokens` instead of hard-coding `None` in `swap_and_burn`. Alternatively, derive an expected output from a governance/oracle-configured reference exchange rate and reject swaps deviating beyond a configured tolerance, so the reward value credited on Bridge Hub cannot be decoupled from the value actually withdrawn from the user.

## Proof of Concept
1. Attacker creates/funds a `pallet_asset_conversion` pool between `tip_asset` and the Ether location (`FeeAsset`/`EthereumLocation`) with modest liquidity, via `AssetConversion::create_pool` + `add_liquidity` (both public, permissionless dispatchables).
2. In a single `pallet-utility::batch_all` extrinsic, attacker calls:
   a. `AssetConversion::swap_exact_tokens_for_tokens` (or `remove_liquidity`) to drain most of the Ether-side reserve, crashing the `tip_asset → Ether` price.
   b. `SnowbridgeSystemFrontend::add_tip(message_id, Asset { id: tip_asset, fun: Fungible(large_amount) })`, which internally calls `swap_and_burn` with `amount_out_min = None` (`lib.rs:301-308`); because the pool is drained, `ether_gained` is near zero despite a large amount of `tip_asset` being withdrawn.
   c. Restore the pool (e.g., reverse swap or `add_liquidity`) within the same batch to normalize price for repeat use.
3. The `AddTip` XCM `Transact` sent to Bridge Hub (built in `build_add_tip_call`) carries the near-zero `amount`, so the relayer reward credited for `message_id` on Bridge Hub is far below what the attacker's `tip_asset` was actually worth, with no minimum-output check ever firing.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L310-316)
```rust
			// Burn the ether
			let ether_asset = Asset::from((ether_location.clone(), ether_gained));

			burn_for_teleport::<T::AssetTransactor>(&origin, &ether_asset)
				.map_err(|_| Error::<T>::BurnError)?;

			Ok(ether_gained)
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs (L50-71)
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
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs (L76-85)
```rust
	type AssetTransactor = AssetTransactors;
	type EthereumLocation = FeeAsset;
	type XcmExecutor = XcmExecutor<XcmConfig>;
	type BridgeHubLocation = SiblingBridgeHub;
	type UniversalLocation = UniversalLocation;
	type PalletLocation = SystemFrontendPalletLocation;
	type Swap = AssetConversion;
	type BackendWeightInfo = weights::snowbridge_pallet_system_backend::WeightInfo<Runtime>;
	type AccountIdConverter = xcm_config::LocationToAccountId;
}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs (L181-199)
```rust
			// Create the pool so the swap will succeed
			let native_asset: Location = Parent.into();
			AssetConversion::create_pool(
				signed_owner.clone(),
				Box::new(native_asset.clone()),
				Box::new(asset.clone()),
			)
			.unwrap();
			AssetConversion::add_liquidity(
				signed_owner,
				Box::new(native_asset),
				Box::new(asset),
				1_000_000_000_000,
				2_000_000_000_000,
				0,
				0,
				asset_owner.into(),
			)
			.unwrap();
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
