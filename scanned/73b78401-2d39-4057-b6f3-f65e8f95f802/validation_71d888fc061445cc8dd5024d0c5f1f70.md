### Title
Missing slippage protection in `swap_and_burn` allows sandwich-attack theft of bridge tip/registration fees - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The Snowbridge system-frontend pallet lets any signed user pay a bridge fee/tip in an arbitrary local asset, which the pallet swaps for Ether via `pallet_asset_conversion` (a public, permissionless AMM) before burning the Ether for teleport to Ethereum. The swap call explicitly passes `None` for the minimum-amount-out parameter, i.e. it has **no slippage protection at all** — a strictly weaker version of the Alchemix `RevenueHandler._melt` bug, which at least enforced `minimumAmountOut == inputAmount`.

### Finding Description
`swap_and_burn` performs the conversion: [1](#0-0) 

Note the call:
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
`amount_out_min` is hard-coded to `None`, so `pallet_asset_conversion::Pallet::do_swap_exact_tokens_for_tokens` skips the `ProvidedMinimumNotSufficientForSwap` check entirely: [2](#0-1) 

This is invoked from two public, unprivileged extrinsics:
- `register_token`, callable by any origin whose location matches the asset owner, and
- `add_tip`, callable by any signed account, with a user-supplied `asset` (the tip) and `message_id`: [3](#0-2) 

Because `pallet_asset_conversion` pools (e.g. on Asset Hub) are open, permissionless AMMs that anyone can trade against in the same block (no relayer, admin, or validator collusion required), an attacker can:
1. Front-run the victim's `add_tip`/`register_token` call by swapping a large amount into the same pool used for `tip_asset -> ether`, moving the price against the victim.
2. Let the victim's call execute with `amount_out_min = None`, so `ether_gained` is computed at the manipulated (unfavorable) price with zero floor — the pallet accepts *any* nonzero output, however small.
3. Back-run by reversing the swap to restore the pool and capture the value difference.

The resulting `ether_gained` (which becomes the actual relayer reward amount forwarded to BridgeHub via `AddTip`/`RegisterToken`, and the Ether that is burned from the user) is far less than fair value, while the user's full `tip_amount` is withdrawn regardless. The victim loses the sandwiched slippage; the attacker captures it — exactly the invariant break described in the external report ("insufficient slippage control ... sandwich ... stealing a portion of the tokens").

Existing guards do not stop this path:
- `pallet_asset_conversion`'s `ProvidedMinimumNotSufficientForSwap` check exists precisely for this purpose but is bypassed because the caller (the frontend pallet) chooses not to supply a minimum.
- No fee-oracle, TWAP, or off-chain quote is used to bound `ether_gained`.
- `add_tip`/`register_token` require only a normal signed origin — no privileged or governance actor is needed, satisfying the "unprivileged attacker" requirement.

### Impact Explanation
This directly causes loss of user funds and mis-priced bridge economic security: the reward amount recorded on BridgeHub for relayers (via `AddTip`) or the fee amount forwarded for Ethereum-side execution (via `RegisterToken`) can be driven arbitrarily low by manipulating the swap pool in the same block, even though the user paid the full intended tip. This can also degrade Snowbridge message delivery economics, since under-priced tips reduce the incentive for relayers to service messages, potentially stalling processing — aligned with the "public underpriced work that degrades ... stalls bridge processing" impact class.

### Likelihood Explanation
High likelihood: `add_tip` is a normal user-facing extrinsic on Asset Hub with no special permissions, the underlying swap pools are public AMMs that anyone can trade in, and sandwiching an on-chain AMM swap within the same block is a well-established, low-cost, capital-available (flash-loan-style) attack pattern — identical in mechanism to the seed report. No malicious relayer, validator, or governance actor is required.

### Recommendation
Require a caller- or protocol-enforced minimum acceptable `ether_gained` (e.g., derived from an oracle price, a TWAP, or a user-supplied slippage-tolerance parameter validated against `QuotePrice`) instead of passing `None` to `swap_exact_tokens_for_tokens` in `swap_and_burn`. At minimum, compute an expected output via `quote_price_exact_tokens_for_tokens` before the swap and enforce a bounded deviation, rejecting the swap (and refunding/aborting the extrinsic) if the realized output falls outside tolerance.

### Proof of Concept
Conceptual reproduction (mirrors the Alchemix PoC structure):
1. Deploy/seed a `pallet_asset_conversion` pool for `(tip_asset, ether_location)` on Asset Hub.
2. Attacker, in the same block, submits a large swap through the same pool to skew the price against `tip_asset -> ether`.
3. Victim calls `EthereumSystemFrontend::add_tip(message_id, tip_asset_amount)`, which internally calls `swap_and_burn` → `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:301-308`), succeeding with a heavily reduced `ether_gained` because no minimum is enforced (contrast with `substrate/frame/asset-conversion/src/tests.rs:1566` `swap_should_not_work_if_too_much_slippage`, which shows the pallet *does* support and test minimum enforcement — but the frontend pallet opts out of it).
4. Attacker reverses their swap in the same block, restoring the pool and pocketing the price difference that would otherwise have gone to the tip/reward.
5. Result: BridgeHub records a much lower `AddTip` reward than the user intended to pay for, and the attacker profits the delta — reproducing the exact economic-loss pattern from the Alchemix report inside Snowbridge's fee-swap path. [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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
