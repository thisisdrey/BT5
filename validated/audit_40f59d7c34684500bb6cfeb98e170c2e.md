### Title
Relayer reward paid on `submit_delivery_receipt` regardless of `DeliveryReceipt.success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` decodes an Ethereum event (`InboundMessageDispatched`) that carries an explicit `success: bool` field, but the pallet never inspects that field before paying out the relayer reward tied to the message's `PendingOrder`.

### Finding Description
The receipt type decoded from the Ethereum gateway log explicitly carries a `success` flag describing whether the dispatched message actually executed successfully on Ethereum: [1](#0-0) 

`submit_delivery_receipt` only verifies the merkle/beacon proof and decodes the log into a `DeliveryReceipt`, then hands off to `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` looks up the `PendingOrder` by `nonce`, and if `order.fee > 0` it unconditionally calls `T::RewardPayment::register_reward` for the full fee, then removes the order — the `receipt.success` field is never read or compared anywhere in the function: [3](#0-2) 

A `grep` across the entire `outbound-queue-v2` pallet source confirms `success` is referenced nowhere outside the doc comment/struct definition — there is no code path that reduces, withholds, or slashes the reward when `success == false`.

This is the direct structural analog of the external report: a function is supposed to gate a payout on whether the "full" outcome was actually achieved (there, `receivedAmount` vs `withdrawAmount - maxLossAmount`; here, `receipt.success` vs. "the message was actually delivered/executed on Ethereum"), but the boolean/condition that should gate the payout is decoded and then silently discarded, so the payout always proceeds as if the condition were satisfied.

### Impact Explanation
Every relayer who submits a valid receipt proof is paid the full `order.fee` (relayer_fee, and per the inbound-queue-v2 analog, tips) regardless of whether the message dispatch on Ethereum actually succeeded. This decouples the reward from a signal the protocol itself defines as meaningful (`success`), so the BridgeHub cannot distinguish "message delivered and executed" from "message delivered but failed to execute" when compensating relayers — value (bridge/reward-pot funds) is paid out identically in both cases, which is a mis-settlement of bridge reward funds relative to the on-chain-defined success semantics.

### Likelihood Explanation
Any unprivileged relayer can trigger this path by simply calling the public `submit_delivery_receipt` extrinsic with a valid proof — no privileged, governance, or malicious-node/relayer assumption is required beyond the normal permissionless relaying role that the protocol already grants to all callers of this extrinsic. The only requirement is a legitimately provable Ethereum event log (which can naturally have `success: false`, e.g. because the dispatched command ran out of gas or reverted downstream), so this is reachable in ordinary operation, not just via an attacker-crafted state.

### Recommendation
Explicitly branch on `receipt.success` in `process_delivery_receipt`: on `success == false`, either withhold/reduce the relayer reward (e.g., pay only a base delivery fee rather than the full order fee) or emit a distinct event so downstream accounting can differentiate failed dispatches, mirroring the external fix pattern of tying the payout condition to the actual verified outcome (`withdrawAmount - maxLossAmount <= receivedAmount` there → `receipt.success` here) instead of unconditionally paying based on the decoded-but-unchecked value.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { fee: F, .. }`.
2. On Ethereum, the gateway processes the message but the dispatched command fails (e.g., insufficient gas budget for the command), causing the gateway to emit `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid proof for this log and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the proof is legitimate), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` reads `order.fee = F > 0` and calls `T::RewardPayment::register_reward(&reward_account, .., F)` — the same as if `success` had been `true` — then removes the order and emits `MessageDelivered`, with no reference anywhere to the `false` value just decoded.

**Uncertainty:** I could not fully verify whether the protocol intends this behavior (i.e., whether the relayer fee is designed purely to compensate for the relaying/proof-submission service and independent of downstream dispatch success, which is a legitimate design in some bridge protocols) or whether `success` was intended as a payout gate but the check was dropped. The code and doc comments in this repository do not state the intended semantics of `success` with respect to reward payment, so this should be validated against the Snowbridge protocol specification before treating it as a confirmed defect rather than an intentional relay-only incentive design.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-27)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}

/// Delivery receipt
#[derive(Clone, Debug)]
pub struct DeliveryReceipt {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// The nonce of the dispatched message
	pub nonce: u64,
	/// Message topic
	pub topic: H256,
	/// Delivery status
	pub success: bool,
	/// The reward address
	pub reward_address: [u8; 32],
}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-317)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

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

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```
