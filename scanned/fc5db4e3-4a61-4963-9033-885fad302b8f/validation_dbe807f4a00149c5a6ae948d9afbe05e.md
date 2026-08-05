Based on the investigation into Snowbridge's reward flow, the closest structural analog to the reported bug (state that promises a reward is bumped without an accompanying, verified transfer/reservation of the underlying value) is the `AddTip` implementation in the outbound-queue-v2 pallet.

### Title
Unbacked fee/reward inflation via `AddTip::add_tip` on `PendingOrders` — ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`Pallet::<T>::add_tip` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` increments `PendingOrders[nonce].fee` purely as a storage mutation, with no accompanying check or on-chain effect that ties the passed `amount` to any value actually held/locked to back it. That same `order.fee` field is later read in `process_delivery_receipt` and passed directly into `T::RewardPayment::register_reward(reward_account, kind, order.fee)` to create a relayer's reward entitlement. This mirrors the reported Solidity pattern where `notifyRewardAmount()`/`distributeRewards()` bumps an accounting counter (`periodState.distributed`) that is treated as "reward available" without any actual transfer of the underlying asset occurring at that point.

### Finding Description [1](#0-0) 

`add_tip` mutates `order.fee = order.fee.saturating_add(amount)` with no interaction with any `Currency`/`fungible` trait, no reservation, and no cross-check against a deposited/locked balance corresponding to `amount`. This function is exposed to the pallet's `AddTip` trait consumer (`snowbridge_pallet_system_v2`), which is driven by user-originated XCM/extrinsic calls (see the `add_tip_from_asset_hub_user_origin` integration test invoking `SnowbridgeSystemFrontend::add_tip` from a normal signed AssetHub account).

Later, in `process_delivery_receipt`: [2](#0-1) 

the accumulated `order.fee` (original message fee + any tips) is turned into a reward credit via `T::RewardPayment::register_reward`. `register_reward` itself is purely bookkeeping — it does not move funds; it just records an entitlement (`RegisteredRewardsCount`/`RegisteredRewardAmount` in the mock, or `RelayerReward` storage in `pallet-bridge-relayers`) that is only settled later when the relayer calls `claim_rewards`/`claim_rewards_to`, at which point `PaymentProcedure::pay_reward` performs the actual transfer (e.g. `PayRewardFromAccount` debiting a rewards pot, or `PayAccountOnLocation` for the `Snowbridge` reward kind).

Because the increment via `add_tip` happens with no verification tying it to an actual locked/transferred value at mutation time, the total "promised" reward recorded in `PendingOrders`/eventually `register_reward` can exceed what the downstream payment procedure can actually back — the same class of break as the report: state is advanced ("reward distributed"/"fee increased") independent of whether backing value was moved, and only surfaces as a failure (or fund lock) at the unrelated, later settlement step.

### Impact Explanation
If the fee/tip bookkeeping and actual balance backing diverge, downstream reward settlement (`pay_reward`) can begin to fail for legitimate relayers (`FailedToPayReward`), since the registered/promised amount no longer matches what the payment procedure can withdraw from its backing pot/location. Because `PendingOrders` is removed from storage once `process_delivery_receipt` runs, and `register_reward` credits are similarly one-shot bookkeeping, there's no fallback path to reconcile or retry with a corrected amount — this can permanently strand a relayer's earned reward or make an unrelated relayer's earlier reward unclaimable due to shared pot exhaustion, i.e. a fund-lock/DoS on bridge reward settlement, aligned with the required "permanent user-fund or bridge-state lock" / "public underpriced work that ... stalls bridge processing" impact classes.

### Likelihood Explanation
`add_tip` is reachable by any account able to submit a signed extrinsic/XCM through `EthereumSystemFrontend::add_tip` (as demonstrated by the `add_tip_from_asset_hub_user_origin` test using an ordinary `AssetHubWestendSender` account) — no privileged/governance origin is required. The only guard visible in the trait implementation itself is `amount > 0` and existence of the pending order; there is no check inside `add_tip` that ties the amount to a corresponding locked/transferred asset. Full confirmation of whether the wrapping `snowbridge_pallet_system_v2::add_tip` extrinsic performs a matching, unit-correct asset withdrawal before invoking this trait method could not be verified within the available code index — this is a genuine gap in my analysis and should be checked directly in that pallet's source (not indexed in the results retrieved) before treating this as a confirmed, exploitable bug rather than a design risk.

### Recommendation
Ensure that `AddTip::add_tip`'s storage mutation is only ever invoked atomically with (and after) a successful, unit-matched withdrawal/reservation of `amount` from the tipping account, ideally by making the fee-charging and `order.fee` increment happen in the same function so a failed charge cannot leave the pending order's fee inflated. Additionally, verify that `order.fee` and any reward-kind conversions preserve currency/unit correctness so that DOT-denominated tips cannot be summed directly into an Ether-denominated relayer reward figure that later fails to settle via `PayAccountOnLocation`.

### Proof of Concept
Not independently reproducible from the indexed code alone: reproducing this concretely requires inspecting `snowbridge_pallet_system_v2::add_tip`'s extrinsic implementation (which calls into `AddTip::add_tip`) to confirm whether/how it charges the tipper before calling the trait method shown above. I could not retrieve that pallet's source in the current session; a Devin session with full repository access would be needed to trace `snowbridge_pallet_system_v2::add_tip` end-to-end and construct a concrete PoC test (e.g. extending `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs`) demonstrating a tipped `order.fee` that exceeds actual backing funds, causing `claim_rewards_to` to fail with `FailedToPayReward` for a legitimately earned reward.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-476)
```rust
			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

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
