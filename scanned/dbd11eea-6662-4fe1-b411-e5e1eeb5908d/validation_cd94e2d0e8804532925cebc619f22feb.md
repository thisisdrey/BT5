### Title
`relayer_fee` in Snowbridge inbound messages is an unbounded, attacker-chosen value registered as a relayer reward with no check against actual bridged value - ([File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs])

### Summary
The 1inch report's core defect is that a value ("cumulative amount") that determines a payout is accepted and recorded into payable state without being bound to anything real (deposited/backing funds), letting the controller of that field inflate payouts arbitrarily. The local analog is in Snowbridge's `snowbridge-pallet-inbound-queue-v2`: the `relayer_fee` field of an inbound `Message` is decoded verbatim from the Ethereum Gateway event log and used directly to increase a relayer's claimable reward balance in `pallet-bridge-relayers`, with no validation that it is bounded by, or proportional to, `message.value` (the actual Ether locked/bridged) or any protocol-defined fee schedule.

### Finding Description
`Message` (decoded in `bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs`, lines 100-120 and `TryFrom<&Log>` at lines 144-180) carries a `relayer_fee: u128` field taken directly from the Solidity event `Payload.relayerFee` emitted by the Ethereum Gateway contract: [1](#0-0) 

`InboundQueue::process_message` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` then takes this `relayer_fee`, adds any pending tip, and unconditionally registers it as a reward for the submitting relayer: [2](#0-1) 

The only checks performed before this point are: the gateway address matches (`Error::InvalidGateway`) and the nonce hasn't been used (`Error::InvalidNonce`) — i.e., authenticity/replay checks on the *message envelope*, not on the *content* of `relayer_fee`. There is no `Config` bound such as `MaxRelayerFee`, no check that `relayer_fee <= message.value`, and no check that `relayer_fee` is proportional to `execution_fee` or to the assets actually transferred. `register_relayer_reward` in `bridges/modules/relayers/src/lib.rs` simply `saturating_add`s whatever value it is given into `RelayerRewards` storage: [3](#0-2) 

Because the Ethereum-side event that produces `relayer_fee` is emitted by whoever calls `v2_sendMessage`/`v2_registerToken` on the Gateway contract (an unprivileged end user, not a Polkadot governance/root actor, not a malicious relayer/prover), any ordinary bridge user can set `relayerFee` to an arbitrary large `u128` value unrelated to how much Ether they actually locked (`value`). Once a legitimate relayer (or the attacker acting as their own relayer, since `submit` is a public, permissionless extrinsic callable by `ensure_signed(origin)`) submits the valid proof for that event, the full `relayer_fee` is credited into `RelayerRewards`, payable later from the bridge's reward/sovereign account via `claim_rewards`/`claim_rewards_to`.

This differs from the excluded "privileged owner/admin abuse" pattern in the seed report: here the corrupting value originates from an *unprivileged* off-chain event field that the runtime blindly trusts once proof-of-existence is verified, and the public `submit`/`process_message` path performs no economic-sanity check on it.

### Impact Explanation
Repeated submission of messages with inflated `relayer_fee` (while `value`/`assets` can be minimal or zero, as shown by test `zero_reward_does_not_register_reward` proving the pallet accepts messages with arbitrary fee/value combinations) drains the reward-paying sovereign account on Bridge Hub / Asset Hub, i.e., theft/unbacked payout of bridge funds to an attacker-controlled relayer account — matching the "theft or unbacked mint or unlock" and "duplicate/incorrect settlement" impact categories for Snowbridge BridgeHub code.

### Likelihood Explanation
Likelihood is high: `submit` is a public extrinsic open to `ensure_signed` origin (any account), and the attacker fully controls the `relayerFee` field of the underlying Ethereum event they emit before relaying it. No relayer collusion, governance, or key compromise is needed — the attacker can act as their own relayer.

### Recommendation
Bound `relayer_fee` in `process_message` before registering the reward, e.g., cap it to a configurable fraction of `message.value`/`execution_fee`, or require that the reward paid never exceeds funds actually backed by the bridged value for that nonce. Add a pallet `Config::MaxRelayerFee` (or a ratio check against `value`) and reject/saturate excessive fees with a new `Error` variant rather than trusting the raw event field.

### Proof of Concept
1. On Ethereum, call the Gateway's `v2_sendMessage` (or equivalent) specifying `Payload.value = 0` (or a small amount) and `Payload.relayerFee = u128::MAX` (or any amount far exceeding the reward pool's real backing).
2. Obtain the event log + valid receipt/execution-header proof (this only proves the event happened, not that the values are sane).
3. Call `EthereumInboundQueueV2::submit(origin, event_proof)` as the attacker's own account, acting as "the relayer".
4. `process_message` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:219-239`) computes `total_tip = relayer_fee.saturating_add(tip)` and calls `T::RewardPayment::register_reward(&relayer, ..., total_tip)`, crediting the attacker's account in `RelayerRewards` for the full inflated amount regardless of the near-zero real `value` transferred.
5. Call `claim_rewards`/`claim_rewards_to` on `pallet-bridge-relayers` to withdraw the inflated reward from the sovereign/reward account, draining funds not backed by any real bridged value.

Note: I was not able to fully verify (given tool-call limits) whether any downstream fee-market or governance-configured spending cap exists elsewhere in the runtime configuration (e.g., in `bridge_common_config.rs` for `bridge-hub-westend`) that might indirectly limit total payouts; if such a global cap exists, it would need to be checked before treating this as fully unmitigated.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L166-176)
```rust
		let message = Message {
			gateway: log.address,
			nonce: event.nonce,
			origin: H160::from(event_payload.origin.as_ref()),
			assets: substrate_assets,
			payload: message_payload,
			claimer,
			value: event_payload.value,
			execution_fee: event_payload.executionFee,
			relayer_fee: event_payload.relayerFee,
		};
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L219-239)
```rust
			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}
```

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
