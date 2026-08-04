## Summary

The external report's core broken invariant is: a public, unprivileged-triggerable payout is computed as `attacker_controlled_price_input × unit_of_work`, and nothing caps how far the attacker can push that price input before the payout is settled. The only mitigation in the original code was an *external* balance ceiling (`balanceLeftForInterval`), not a bound on the price input itself.

The closest local analog in this repo is Snowbridge's `snowbridge-pallet-system-frontend::add_tip` (and `register_token`), which converts a user-supplied tip asset into Ether via an on-chain AMM swap with **no minimum-output protection**, and forwards the resulting (attacker-influenceable) `ether_gained` value as the exact amount credited to `Tips`/reward state on BridgeHub — ultimately paid out to a relayer via `pallet_bridge_relayers`/`RewardPayment::register_reward`.

## Finding Description

### Title
Unbounded relayer-reward inflation via unprotected AMM swap in `snowbridge-pallet-system-frontend::add_tip` - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

`add_tip` lets any signed account convert an arbitrary fungible asset into Ether to fund a relayer tip/reward for a Snowbridge message: [1](#0-0) 

The conversion happens in `swap_fee_asset_and_burn` → `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens` with **`None` as the minimum-amount-out**: [2](#0-1) 

The returned `ether_gained` — an AMM spot-price-dependent value with zero floor/ceiling check — is burned for teleport and then forwarded, unmodified, as the exact tip/reward `amount` to the backend pallet: [3](#0-2) 

On BridgeHub, this `amount` is added directly into `Tips`/`PendingOrders.fee` with no re-validation of "real world" value, and is later paid out in full as relayer reward: [4](#0-3) [5](#0-4) [6](#0-5) 

This mirrors the Aggregator bug exactly: a value that funds a payout (`tx.gasprice` there, AMM spot price here) is fully attacker-influenceable at call time, and the payout formula (`gas_spent * price` there, `fee_amount` swapped through an AMM here) has no independent bound tying it to the *actual* value contributed — only a generic balance/interval cap exists downstream (there, `balanceLeftForInterval`; here, nothing comparable at all — no per-tip cap, no oracle cross-check against `fee_per_gas`/pricing parameters used elsewhere in the same codebase for the primary fee calculation, e.g. `PricingParameters` in `bridges/snowbridge/primitives/core/src/pricing.rs`).

## Impact Explanation

An unprivileged user can:
1. Deposit a large amount of a low-liquidity asset (or one they control the pool for) paired against Ether/the tip target asset in `pallet_asset_conversion`.
2. Skew the pool reserves (e.g., via `add_liquidity`/`remove_liquidity` or an unbalancing swap in the same extrinsic batch) so that a subsequent unit of the tip asset swaps for far more Ether than its real economic value.
3. Call `add_tip` with a small nominal `fee_amount`, receive an inflated `ether_gained` due to the skewed pool and `None` min-out, and have that inflated amount registered as a real relayer reward/tip that is ultimately paid from the bridge's reward ledger.
4. Claim the reward themselves (as the relayer who processes/confirms the message), extracting value that was never actually backed by an equivalent amount of real assets.

This is a theft/unbacked-mint impact on bridge-held value: `Tips`/`PendingOrders.fee` and the resulting `RewardPayment::register_reward` amounts do not conserve value, since the "price" (AMM spot rate) used to compute the payout is briefly attacker-controlled and unchecked.

## Likelihood Explanation

Requires no validator, collator, relayer, or governance collusion — a single unprivileged account with capital to temporarily move a thin liquidity pool (or one it seeds itself) can trigger this in a single atomic transaction/batch. The absence of `min_amount_out` (`None` is hardcoded, not attacker-supplied but also not derived from any reference price) removes the standard AMM defense against this exact class of manipulation.

## Recommendation

- Pass a real `min_amount_out` to `swap_exact_tokens_for_tokens` derived from a trusted reference price (similar to `PricingParameters::exchange_rate` used elsewhere in Snowbridge for fee calculation), or route conversion through a pallet-controlled oracle price instead of raw pool spot price.
- Enforce a hard cap or sanity bound on the maximum tip/reward addable per message, decoupled from raw AMM output, consistent with the original `saveResult` remediation of "place a constant boundary on spent gas."

## Proof of Concept

1. Attacker seeds/controls an `AssetConversion` pool pairing `TipAssetX` with `EtherLocation` with thin liquidity.
2. In a single `utility.batch_all` (or successive same-block extrinsics), attacker skews reserves in their favor, then calls `SnowbridgeSystemFrontend::add_tip(origin, message_id, Asset{ id: TipAssetX, fun: Fungible(small_amount) })`.
3. `swap_and_burn` executes `swap_exact_tokens_for_tokens(..., min_amount_out: None, ...)` against the skewed pool, returning `ether_gained` far above `small_amount`'s true value.
4. `ether_gained` is burned and forwarded via `build_add_tip_call` to BridgeHub's `EthereumSystem::add_tip`, credited into `Tips`/`PendingOrders.fee`.
5. Attacker (or an accomplice) relays/confirms the corresponding message and receives `RewardPayment::register_reward(&relayer, ..., total_tip)` for the inflated amount, extracting more value than was actually contributed — a direct analog of the Aggregator's unbounded gas-price rebate exploit.

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L234-239)
```rust
			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```
