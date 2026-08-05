## Title
Snowbridge outbound-queue-v2 pays relayer rewards regardless of the delivery-receipt's `success` flag - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
The Swafe finding is a state-tracking/lookup mismatch: an item is transitioned into a new logical state (`recover`), but the acceptance check for the follow-on step still trusts the *old* state (`backups`), so the pallet either loses track of the item or accepts/rejects the wrong thing. Searching for the same class of defect in `paritytech/polkadot-sdk` — "state field exists to gate an action, but the action's implementation never consults it" — the strongest local analog is `Pallet::process_delivery_receipt` in the Snowbridge V2 outbound queue: it decodes a `DeliveryReceipt` that carries an explicit `success: bool` field describing whether the message actually executed on Ethereum, but the reward-payment code path never reads or checks that field before releasing the relayer's reward.

## Finding Description
`DeliveryReceipt` (decoded from the `InboundMessageDispatched` Ethereum event) explicitly carries a `success` flag: [1](#0-0) 

`process_delivery_receipt` verifies the event/proof, decodes the receipt, looks up the `PendingOrder` purely by `nonce`, and pays the reward whenever `order.fee > 0` — with no reference anywhere to `receipt.success`: [2](#0-1) 

This mirrors the Swafe bug pattern precisely: a piece of state that is supposed to gate whether a downstream action is valid (`recover` queue membership in Swafe / `success` flag here) is captured and stored, but the consuming code path reads/accepts based on a different, coarser condition (`backups` list in Swafe / mere existence of a `PendingOrder` keyed by nonce here) instead of the authoritative flag. In both cases the guard that should decide "is this claim/state actually valid" is bypassed because the check was wired to the wrong field/collection.

`process_delivery_receipt` is a public, unprivileged, permissionless entry point (`submit_delivery_receipt` extrinsic, `ensure_signed` only) that ordinary relayers call: [3](#0-2) 

The only binding done before paying is on `gateway` and `nonce`; there is no check that the message actually dispatched successfully on the Ethereum side, and no check that binds the receipt to the specific `topic`/message content beyond nonce lookup.

## Impact Explanation
Reward funds (`T::RewardPayment::register_reward`) are unconditionally credited for every valid, verified event log with a matching nonce and gateway, irrespective of whether Ethereum execution of the corresponding command actually succeeded. Since `success: false` is a legitimate outcome that the Ethereum gateway contract emits (e.g., a message whose commands revert on execution), a relayer that merely delivers a message that fails on-chain still collects the full fee as if it had succeeded. This is a direct instance of "duplicate settlement or payout"/"theft of unbacked/undue value" from the bridge reward pool — value is settled to the relayer without the delivery outcome that the fee is supposed to be conditioned on, and the `PendingOrder` is removed either way, so there is no path to re-attempt or dispute a failed delivery for reward purposes.

## Likelihood Explanation
The path requires no privileged actor, malicious relayer collusion, or forged proof — only a normal relayer submitting a legitimate `EventProof` for an Ethereum transaction that emitted `success: false` (which can legitimately occur due to gas exhaustion, command-level revert, etc., independent of relayer honesty). `T::Verifier::verify` only attests that the log is real and included; it says nothing about the `success` bool's implication for payment eligibility, so any relayer who observes (or intentionally engineers, e.g. by controlling command parameters that revert) a failed dispatch on Ethereum can still collect the reward by submitting the resulting receipt.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before crediting `T::RewardPayment::register_reward`: only pay (or pay in full) when `success == true`; for `success == false`, either withhold/reduce the reward, or route to a separate accounting path (e.g., partial reward for correct relaying but no dispatch-success bonus, mirroring how `pallet-bridge-messages` conditions reward on dispatch results elsewhere in the repo). Regardless of the resolution, `receipt.success` must be read and enforced, and the intended semantics (should relayers be paid purely for delivering the proof, or only for successful on-chain dispatch) should be made explicit in code and covered by a regression test asserting no `RewardRegistered`/reduced reward on `success: false`.

## Proof of Concept
1. A message with `fee > 0` is queued and processed via `do_process_message`, creating `PendingOrders[nonce] = PendingOrder { nonce, fee, block_number }`. [4](#0-3) 
2. On Ethereum, the Gateway contract executes the message's commands and they revert/fail; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer builds an `EventProof` for this real log and calls `submit_delivery_receipt`. `T::Verifier::verify` succeeds (it's a genuine event), and `DeliveryReceipt::try_from` decodes `success = false`. [5](#0-4) 
4. `process_delivery_receipt` finds `order.fee > 0`, calls `register_reward` unconditionally, and removes the `PendingOrder` — the relayer is paid in full despite the on-chain dispatch failure, and there is no state left to reconcile or claw back the reward. [6](#0-5)

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L14-27)
```rust
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-51)
```rust
impl TryFrom<&Log> for DeliveryReceipt {
	type Error = DeliveryReceiptDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-440)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });
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
