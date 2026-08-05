### Title
Register-token / relayer-tip fee conversion trusts unguarded, manipulable AMM spot price with no minimum-output or deviation check - ([File: bridges/snowbridge/pallets/system-frontend/src/lib.rs])

### Summary
`pallet-snowbridge-system-frontend` converts a user-supplied fee/tip asset into Ether by calling the on-chain `AssetConversion` swap with **no minimum output amount and no reference-price sanity check**, then feeds that unauthenticated result directly into cross-chain accounting (`register_token`'s anti-spam deposit and `add_tip`'s relayer reward). This is a direct structural analog of the reported flaw: the ecosystem relies on a single, unvalidated price source (`IGmxVault`) with no deviation-from-Chainlink check and no staleness guard. Here the "oracle" is the parachain's own `pallet_asset_conversion` pool spot price, queried via `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)`.

### Finding Description
`Pallet::<T>::swap_and_burn` (called by both `register_token` and `add_tip`, both permissionless, unprivileged extrinsics) performs: [1](#0-0) 

Note the swap invocation passes `None` for the minimum-amount-out parameter — there is no slippage bound, no comparison to a trusted reference exchange rate (unlike `snowbridge_pallet_outbound_queue`'s `PricingParameters.exchange_rate`, which is a governance-set, validated value used elsewhere in the bridge), and no staleness/deviation check of the pool price at all.

The resulting `ether_gained` — entirely a function of the current AMM pool reserves, which any account can move via ordinary `swap`/`add_liquidity`/`remove_liquidity` calls on `pallet_asset_conversion` — is then used unchecked to:
- Set the anti-spam `amount` parameter forwarded in the `register_token` Transact call to `EthereumSystem::register_token` on BridgeHub: [2](#0-1) [3](#0-2) 
- Set the relayer reward top-up amount forwarded via `add_tip`: [4](#0-3) [5](#0-4) 

This mirrors exactly the pattern flagged in the external report: a "sole price oracle" (here, one permissionless AMM pool) is trusted for an economically-security-relevant amount, with no deviation check against any external reference and no minimum/floor enforced on the swap output.

### Impact Explanation
`register_token`'s Ether payment is explicitly documented elsewhere as existing "to discourage spamming" (`register_token: U256` field comment in `Command::SetTokenTransferFees`): [6](#0-5) 
Because the DOT→ETH conversion used to compute that anti-spam amount is taken from a live, permissionless, single-source AMM price with zero slippage/staleness protection, an unprivileged actor can transiently skew the pool (e.g., via `add_liquidity`/`remove_liquidity` or a large one-sided swap they control) to make a trivial DOT/asset amount convert into an inflated `ether_gained`, satisfying (or bypassing the economic intent of) the anti-spam fee while paying far less real value than intended. This is "public underpriced work that degrades block production or stalls bridge processing," since it lets an attacker cheaply spam Ethereum-side `register_token`/`add_tip` Transact-dispatch commands that consume BridgeHub outbound queue capacity and Ethereum gateway processing, without needing any admin, validator, relayer, or governance compromise.

### Likelihood Explanation
Both `register_token` and `add_tip` are open to any signed account; `pallet_asset_conversion` pools are themselves open to any account to add/remove liquidity or swap. No governance, admin, or privileged role is required — the attacker only needs enough capital to momentarily move the pool used for the fee-asset/ETH pair, call the vulnerable extrinsic in the same or adjacent block, then reverse their pool position. The absence of a minimum-output parameter (`None`) and absence of any independent reference-rate comparison (contrast with `PricingParameters::validate` used in the outbound-queue v1 fee pipeline, which at least enforces non-zero exchange rates set by root) make this straightforward to trigger without any front-running of a third party.

### Recommendation
- Pass a computed minimum-output (slippage-bounded) amount to `T::Swap::swap_exact_tokens_for_tokens` instead of `None`.
- Cross-check the AMM-derived conversion against a governance-set reference rate (as already exists for `snowbridge_pallet_outbound_queue::PricingParameters`) and reject/clamp if deviation exceeds a configured threshold.
- Consider using a time-weighted average price (TWAP) from the pool rather than instantaneous spot price for any amount that feeds anti-spam fees or relayer rewards.

### Proof of Concept
1. Attacker identifies the `pallet_asset_conversion` pool between the fee asset (e.g. DOT) and Ether used by `swap_and_burn`.
2. Attacker calls `AssetConversion::add_liquidity`/`swap` to skew reserves so that a small amount of DOT quotes to a large amount of Ether.
3. Attacker calls `EthereumSystemFrontend::register_token` (or `add_tip`) with a minimal `fee_asset`/`asset` amount; `swap_fee_asset_and_burn`/`swap_and_burn` executes with `None` minimum-out and returns an inflated `ether_gained`.
4. Attacker reverses the pool skew (removes liquidity / swaps back), recovering most of their capital.
5. The inflated `ether_gained` is used unchecked as the anti-spam `amount` for `register_token` (or reward top-up for `add_tip`), which is forwarded via Transact to BridgeHub/Ethereum, allowing cheap spam of token-registration/relayer-reward commands relative to the intended economic cost. [1](#0-0)

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L82-90)
```rust
	/// Set token fees of the Gateway contract
	SetTokenTransferFees {
		/// The fee(DOT) for the cost of creating asset on AssetHub
		create_asset_xcm: u128,
		/// The fee(DOT) for the cost of sending asset on AssetHub
		transfer_asset_xcm: u128,
		/// The fee(Ether) for register token to discourage spamming
		register_token: U256,
	},
```
