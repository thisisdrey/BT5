Based on the evidence gathered, I found a concrete local analog in the bridge relayers reward-accounting pallet.

### Title
Unbounded cumulative relayer reward accumulation silently saturates instead of reverting, causing permanent loss of excess reward value - (File: `bridges/modules/relayers/src/lib.rs`)

### Summary
The external report describes a bridge that checks each individual outbound transfer against a `uint64` cap in shared decimals, but never tracks the *cumulative* outbound amount, so many small transfers can together exceed the destination chain's representable maximum and silently corrupt state instead of reverting. The local analog is `register_relayer_reward` in the Bridge Relayers pallet, which accumulates a relayer's pending reward balance across many message-delivery events using `saturating_add` with no check against, or protection for, the `T::RewardBalance` numeric ceiling. Each individual reward registration is bounded/sane, but the pallet never validates the *cumulative* stored value against `T::RewardBalance::MAX`, and uses saturating arithmetic rather than a checked/reverting path.

### Finding Description
`register_relayer_reward` mutates the `RelayerRewards` double map by taking the old stored reward (or zero) and doing `old_reward.unwrap_or_else(Zero::zero).saturating_add(reward_balance)` [1](#0-0)  This function is invoked from `register_relayers_rewards` in the payment adapter every time a batch of messages is delivered/confirmed, where each individual relayer's reward for the batch is computed as `T::RewardBalance::saturated_from(messages).saturating_mul(delivery_fee)` [2](#0-1) .

Because the accumulation step uses `saturating_add` rather than `checked_add` with an error path, there is no cumulative tracking or cap enforcement analogous to what the report recommends: an ever-growing stored balance across many delivery batches (submitted over time by an unprivileged relayer/attacker who can control the pace and volume of message deliveries) can approach `T::RewardBalance::MAX`. Once it does, further legitimate reward registrations are silently clipped at the max value rather than causing a revert or an alert, so the surplus reward value is permanently and silently lost — the pallet has no equivalent of the report's recommended "track total amount, revert if over cap" logic.

### Impact Explanation
If the stored `RelayerRewards` value saturates, the relayer permanently loses any reward amount beyond `T::RewardBalance::MAX`, and this happens silently with no error, event, or dispatch failure signaling the truncation. This is a permanent fund-loss condition matching the "permanent user-fund ... lock" impact category, since paid-out relayer rewards are effectively capped and any excess is unrecoverable once truncated.

### Likelihood Explanation
Likelihood is low: `T::RewardBalance` is typically a `u128`-based type in production configurations, so reaching the saturation point requires an enormous number of message deliveries/relayed batches accumulated without an intervening claim. This mirrors the report's own "Low likelihood / High impact" classification for its `uint64` cumulative-overflow scenario.

### Recommendation
Replace the `saturating_add` accumulation in `register_relayer_reward` with a `checked_add`, and either fail loudly (deposit an error event / require the relayer to claim rewards before the cap is approached) or track the cumulative total explicitly and reject/revert further reward registration once the value would exceed `T::RewardBalance::MAX`, rather than silently truncating.

### Proof of Concept
1. An attacker-controlled relayer repeatedly triggers message-delivery/confirmation flows that credit `register_relayer_reward` via `register_relayers_rewards` [3](#0-2) , without calling `claim_rewards` to withdraw the accrued balance.
2. Over many iterations, `RelayerRewards::<T, I>::mutate(...)` keeps applying `saturating_add` [4](#0-3) , so the stored value approaches `T::RewardBalance::MAX`.
3. Once saturation is reached, subsequent legitimate reward registrations for that relayer are silently dropped (the stored value no longer increases), and calling `claim_rewards` will only ever pay out the saturated (truncated) amount, permanently losing the excess reward value with no error signaled anywhere in the code path.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L399-416)
```rust
		/// Register reward for given relayer.
		pub(crate) fn register_relayer_reward(
			reward_kind: T::Reward,
			relayer: &T::AccountId,
			reward_balance: T::RewardBalance,
		) {
			if reward_balance.is_zero() {
				return;
			}

			RelayerRewards::<T, I>::mutate(
				relayer,
				reward_kind,
				|old_reward: &mut Option<T::RewardBalance>| {
					let new_reward =
						old_reward.unwrap_or_else(Zero::zero).saturating_add(reward_balance);
					*old_reward = Some(new_reward);

```

**File:** bridges/modules/relayers/src/payment_adapter.rs (L77-103)
```rust
// Update rewards to given relayers, optionally rewarding confirmation relayer.
fn register_relayers_rewards<
	T: Config<RI> + pallet_bridge_messages::Config<MI>,
	RI: 'static,
	MI: 'static,
>(
	confirmation_relayer: &T::AccountId,
	relayers_rewards: RelayersRewards<T::AccountId>,
	lane_id: RewardsAccountParams<LaneIdOf<T, MI>>,
	delivery_fee: T::RewardBalance,
) where
	<T as Config<RI>>::Reward: From<RewardsAccountParams<LaneIdOf<T, MI>>>,
{
	// reward every relayer except `confirmation_relayer`
	let mut confirmation_relayer_reward = T::RewardBalance::zero();
	for (relayer, messages) in relayers_rewards {
		// sane runtime configurations guarantee that the number of messages will be below
		// `u32::MAX`
		let relayer_reward =
			T::RewardBalance::saturated_from(messages).saturating_mul(delivery_fee);

		if relayer != *confirmation_relayer {
			Pallet::<T, RI>::register_relayer_reward(lane_id.into(), &relayer, relayer_reward);
		} else {
			confirmation_relayer_reward =
				confirmation_relayer_reward.saturating_add(relayer_reward);
		}
```
