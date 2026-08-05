### Title
Missing slippage protection in Snowbridge `system-frontend` tip/fee swap enables sandwich attacks that under-fund relayer rewards - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`pallet_asset_conversion::Swap::swap_exact_tokens_for_tokens` exposes an `amount_out_min: Option<Balance>` parameter that, when `Some`, causes the swap to fail with `Error::<T>::ProvidedMinimumNotSufficientForSwap` if the AMM pool returns less than expected [1](#0-0) . In `bridges/snowbridge/pallets/system-frontend/src/lib.rs`, this protection is explicitly disabled: `swap_and_burn` calls the swap with `None` and a code comment stating "No minimum amount required" [2](#0-1) .

### Finding Description
Two public, unprivileged extrinsics route through this unprotected swap:
- `add_tip(origin, message_id, asset)` — any signed account can call this to convert an arbitrary tip asset into ether via `swap_fee_asset_and_burn` → `swap_and_burn` [3](#0-2) .
- `register_token(origin, asset_id, metadata, fee_asset)` — for any non-root origin, the fee asset is likewise swapped for ether before dispatch [4](#0-3) .

Because `amount_out_min` is hard-coded to `None`, the on-chain AMM swap in `pallet_asset_conversion` will accept any non-zero output amount, however small, with no guard against price manipulation within the same or adjacent blocks (i.e., a sandwich: front-run to move the pool price against the victim's swap, let the victim swap execute at a degraded price, back-run to restore the price and capture the difference) [5](#0-4) . This is the same root-cause pattern as the referenced report: a swap/price-dependent code path with no minimum-output enforcement, making it fully exposed to AMM price impact and sandwich manipulation — except here there is *no* slippage bound at all, whereas the original report's `CrvDepositorWrapper` at least attempted a TWAP-based `minOut`.

The resulting `ether_gained` (the manipulated, artificially-low swap output) is then:
1. Burned via `burn_for_teleport` [6](#0-5) , and
2. Forwarded as the `amount` in the `EthereumSystemCall::AddTip`/`RegisterToken` XCM `Transact` sent to BridgeHub [7](#0-6) , where it is credited as the relayer reward tip for message `message_id`.

### Impact Explanation
Because the reward credited on BridgeHub is directly derived from an unprotected AMM swap output, an attacker (any unprivileged account with access to the same pool, no special node/relayer/validator/governance role required) can sandwich a victim's `add_tip` call to reduce the ether amount actually recorded as reward for a specific outbound message. This is "public underpriced work" in the bridge delivery-fee sense: relayers may see an under-funded reward for delivering that message, disincentivizing timely relay and stalling that message's processing on the Ethereum side, and the tip payer permanently loses the difference to the attacker (fund loss, not merely front-run-only price movement, since there is zero minimum-output floor rather than a merely-imprecise one).

### Likelihood Explanation
`pallet_asset_conversion` pools on parachains like Asset Hub are typically shallow relative to Ethereum-side DeFi, and `add_tip`/`register_token` are ordinary user-facing calls (no permissioning) whose swap path and amount are visible pre-execution, making sandwiching straightforward for any actor able to submit adjacent transactions in the same block — no malicious relayer, collator, or governance role is needed.

### Recommendation
Compute and pass a non-`None` `amount_out_min` in `swap_and_burn` (e.g., derived from `T::Swap`/`QuotePrice::quote_price_exact_tokens_for_tokens` with a configurable slippage tolerance, or an explicit user-supplied minimum passed through the extrinsic), so a manipulated pool price causes the swap to fail (`ProvidedMinimumNotSufficientForSwap`) rather than silently returning a degraded, attacker-favorable amount.

### Proof of Concept
1. Attacker observes a pending `add_tip` extrinsic for a large `tip_asset` amount against a thinly-liquid `tip_asset → ether` pool in `pallet_asset_conversion`.
2. Attacker front-runs with a large swap in the same direction to move the pool price against the tip asset.
3. Victim's `add_tip` executes via `swap_and_burn` with `amount_out_min = None` [2](#0-1) , so it succeeds despite yielding far less ether than a fair-price quote would produce; there is no `ProvidedMinimumNotSufficientForSwap` check to block it.
4. Attacker back-runs to reverse their initial swap, extracting the price-impact profit.
5. The under-valued `ether_gained` is burned and transmitted as the tip/reward amount in `EthereumSystemCall::AddTip` to BridgeHub [7](#0-6) , permanently under-funding the relayer reward for that message while the victim's paid-in tip asset is fully consumed.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L989-1002)
```rust
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L147-172)
```rust
impl<T: Config> Swap<T::AccountId> for Pallet<T> {
	type Balance = T::Balance;
	type AssetKind = T::AssetKind;

	fn max_path_len() -> u32 {
		T::MaxSwapPathLength::get()
	}

	#[transactional]
	fn swap_exact_tokens_for_tokens(
		sender: T::AccountId,
		path: Vec<Self::AssetKind>,
		amount_in: Self::Balance,
		amount_out_min: Option<Self::Balance>,
		send_to: T::AccountId,
		keep_alive: bool,
	) -> Result<Self::Balance, DispatchError> {
		Self::do_swap_exact_tokens_for_tokens(
			sender,
			path,
			amount_in,
			amount_out_min,
			send_to,
			keep_alive,
		)
	}
```
