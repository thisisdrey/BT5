Found a direct analog in `bridges/snowbridge/pallets/system-frontend/src/lib.rs`.

### Title
Unbounded-slippage swap in Snowbridge `system-frontend` fee/tip conversion allows sandwich-based theft of user tip/fee assets - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The `register_token` and `add_tip` extrinsics convert a user-supplied fee/tip asset into Ether via `Pallet::swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None`. This mirrors exactly the bug class in the external report: a swap executed on behalf of a user with no floor on the output amount, permissionlessly triggerable, executed against an on-chain AMM pool (`pallet_asset_conversion`) whose reserves any account can move beforehand.

### Finding Description
`swap_and_burn` builds the swap path `[tip_asset_location, ether_location]` and invokes: [1](#0-0) 
passing `None` explicitly as the `amount_out_min` argument, with the comment "No minimum amount required". This is a public, non-privileged entrypoint: `add_tip` is callable by any signed account and immediately routes into `swap_fee_asset_and_burn` → `swap_and_burn`: [2](#0-1) [3](#0-2) 

The underlying `pallet_asset_conversion::Swap::swap_exact_tokens_for_tokens` trait explicitly supports `amount_out_min: Option<Self::Balance>` specifically to prevent this class of attack — the pallet's own dispatchable enforces `ProvidedMinimumNotSufficientForSwap` when a minimum is supplied: [4](#0-3) 
But `system-frontend` deliberately opts out of that protection by passing `None`, so the check at lines 997-1002 of `asset-conversion/src/lib.rs` is skipped entirely, and the swap will settle at whatever price the pool currently offers — including a price crashed by a large trade placed immediately beforehand in the same block or a preceding block.

This is functionally identical to the `get_lp_by_cake` issue: a permissionless function that moves the caller's funds through an AMM swap with an attacker-controllable price, and no oracle/slippage floor to bound the loss.

### Impact Explanation
The `ether_gained` value directly determines the amount of Ether that gets burned/teleported and credited as a relayer reward or registration fee funding amount on the Ethereum side (`build_register_token_call`'s `amount` field, and `build_add_tip_call`'s `amount` field, both consumed by BridgeHub as the reward/fee basis): [5](#0-4) [6](#0-5) 
An attacker can manipulate the relevant `pallet_asset_conversion` pool reserves (via ordinary swap/liquidity-removal calls available to anyone — no privileged actor required) immediately before the victim's `add_tip`/`register_token` call executes, causing the victim's tip/fee asset to be swapped for far less Ether than the fair price. The user's tip asset is consumed at withdrawal-time amount but the resulting `ether_gained` (and thus the relayer reward / bridge fee funding) can be manipulated to be far lower than intended, degrading Snowbridge's public relayer-incentive mechanism and enabling attackers to extract value at the expense of the tipping user, aligning with the "public underpriced work that... stalls bridge processing" and "duplicate/incorrect settlement amount" impact categories.

### Likelihood Explanation
No privileged role, governance action, or malicious relayer/validator is required — an ordinary user can front-run/sandwich another ordinary user's `add_tip`/`register_token` call in the mempool of the parachain hosting `pallet_asset_conversion`, or exploit natural pool thinness. The `None` is hard-coded in the pallet source (not a caller-supplied parameter), so every invocation of `add_tip` and `register_token` (for non-root, non-Ether-fee-asset payers) is unconditionally exposed.

### Recommendation
Compute an `amount_out_min` before swapping — e.g., via `QuotePrice::quote_price_exact_tokens_for_tokens` (already used elsewhere in the codebase, such as `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`) with an acceptable slippage tolerance, and pass `Some(min_out)` to `swap_exact_tokens_for_tokens` instead of `None`. Alternatively, expose a caller-supplied `min_ether_out` parameter on `add_tip`/`register_token` so the tipping user can bound their own slippage risk.

### Proof of Concept
1. Attacker observes a pending `add_tip(message_id, asset)` extrinsic (or `register_token`) in the transaction pool of the parachain hosting the `tip_asset_location`/`ether_location` pool in `pallet_asset_conversion`.
2. Attacker submits a large swap (or removes most liquidity) against the same pool just before the victim's extrinsic, using the pallet's own public `swap_exact_tokens_for_tokens`/`remove_liquidity` extrinsics — no privilege needed.
3. The victim's `add_tip` executes `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)` [1](#0-0) , filling at the manipulated, degraded price, since no `amount_out_min` guard exists to abort the swap.
4. Attacker reverses their position (re-adds liquidity / swaps back) after the victim's transaction, extracting the difference between the fair and manipulated price, which was effectively taken from the victim's tip/fee value as `ether_gained`.

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
