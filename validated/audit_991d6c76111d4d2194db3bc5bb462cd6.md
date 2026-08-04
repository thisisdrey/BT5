### Title
Outbound Queue V2 accepts an unvalidated, attacker-controllable `fee` for real delivery work, allowing zero relayer compensation - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs`, `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The reported Ext01Handler bug is a value-vs-compensation mismatch: the contract hardcodes the keeper's execution fee to `0` regardless of the caller-supplied `executionFee`, so the keeper performs real work but is paid nothing. The `snowbridge-pallet-outbound-queue-v2` has a structurally identical flaw: the `Message.fee` field, which becomes the relayer's sole compensation (`PendingOrder.fee`), is taken directly from caller-supplied data with **no minimum-fee validation anywhere in the pipeline**, so a message can be queued, committed, and require real off-chain relayer work to Ethereum while the relayer receives zero reward.

### Finding Description
`SendMessage::validate` for the v2 outbound queue only checks the payload size and does not touch or bound `message.fee` at all: [1](#0-0) 

`Pallet::do_process_message` decodes the `Message` (including `fee`) and stores it verbatim into `PendingOrder.fee`, again with no lower bound check: [2](#0-1) 

When the relayer later submits the delivery receipt, the pallet only pays a reward if `order.fee > 0`: [3](#0-2) 

This mirrors the Ext01Handler pattern exactly: the contract/pallet accepts and commits to doing real settlement/delivery work (message committed into the merkle root, requiring finality proof and gas expenditure by an off-chain relayer on Ethereum), but the compensation value stored for that work can be `0` because nothing enforces `fee >= minimum`. Unlike `pallet-bridge-relayers`' `DeliveryConfirmationPaymentsAdapter`, which computes reward as `messages * delivery_fee` from a governance-configured constant [4](#0-3) , the v2 outbound queue lets the message's own `fee` field — set by the message originator (XCM exporter or `pallet-system-v2::send`) — pass through unchecked into the reward path.

The existing `AddTip` mechanism only adds on top of `order.fee`; it does nothing to guarantee a floor, and there is no requirement that a tip be added before a message is committed: [5](#0-4) 

### Impact Explanation
If a message is enqueued with `fee: 0` (or any negligible value), it is still committed into the Merkle root and included in the header digest, requiring a relayer to expend real gas relaying and executing it on Ethereum. Because reward payment is skipped entirely for `fee == 0`, this is public underpriced work: real bridge-delivery labor with no guaranteed compensation, which can suppress relayer participation and stall/degrade message delivery from BridgeHub to Ethereum — directly matching the "public underpriced work that degrades block production or stalls bridge processing" impact category for Snowbridge.

### Likelihood Explanation
Any component that can enqueue a v2 outbound message (the `EthereumBlobExporter` XCM converter or `pallet-system-v2::send`) determines the `fee` value baked into the `Message`. Because `validate()` performs no minimum-fee check, any caller path that does not itself enforce a floor (e.g. a misconfigured or permissively-filtered XCM route) can produce zero/near-zero fee messages that still get committed and require relaying — no privileged/malicious relayer, validator, or governance actor is needed to trigger this; the flaw exists purely in the pallet's fee-acceptance logic.

### Recommendation
Enforce a minimum fee at the point of message validation/enqueue, analogous to the fix applied to `Ext01Handler`:
- In `SendMessage::validate` (or `do_process_message`), reject or reprice messages whose `fee` is below a configured minimum required to compensate expected relayer gas cost, instead of silently accepting `fee: 0`.
- Alternatively, require `PendingOrder.fee` to be clamped to a `MinimumDeliveryFee` config constant rather than trusting the raw `Message.fee` value, and emit an error/`SendError` when the supplied fee cannot meet this floor.

### Proof of Concept
1. Construct a `Message` with `fee: 0` and a valid `commands` payload within `MaxMessagePayloadSize`.
2. Call `SendMessage::validate` — it succeeds because only payload size is checked (`send_message_impl.rs` lines 23-32).
3. `deliver` enqueues the message; `do_process_message` runs, storing `PendingOrder { fee: 0, .. }` and committing the message into the Merkle root (`lib.rs` lines 390-436) — this message now requires real off-chain relaying to Ethereum.
4. A relayer performs the actual delivery work, submits `submit_delivery_receipt`, and `process_delivery_receipt` is invoked; since `order.fee == 0`, the `if order.fee > 0` guard skips `T::RewardPayment::register_reward` entirely (`lib.rs` lines 464-473), so the relayer receives nothing for real, verified delivery work.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-32)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		Ok(message.clone())
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L464-473)
```rust
			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-495)
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
```

**File:** bridges/modules/relayers/src/payment_adapter.rs (L90-96)
```rust
	// reward every relayer except `confirmation_relayer`
	let mut confirmation_relayer_reward = T::RewardBalance::zero();
	for (relayer, messages) in relayers_rewards {
		// sane runtime configurations guarantee that the number of messages will be below
		// `u32::MAX`
		let relayer_reward =
			T::RewardBalance::saturated_from(messages).saturating_mul(delivery_fee);
```
