## Analysis

The `ArmadaTreasuryGov` report’s core broken invariant is: **a value-conserving/settlement action that the protocol’s own data model promises to gate on a specific condition is instead executed unconditionally, because the enforcement code for that condition was never wired in.** The closest verifiable local analog in this repository is in the Snowbridge V2 outbound-queue relayer-reward settlement path, where the `DeliveryReceipt.success` field is decoded from a verified Ethereum event but is never consulted before the relayer reward is credited.

### Title
Relayer reward settlement in `EthereumOutboundQueueV2::process_delivery_receipt` ignores the decoded `success` flag - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`DeliveryReceipt` — decoded from the Ethereum `InboundMessageDispatched` log — carries a `success: bool` field describing whether the outbound message actually executed successfully on the Gateway contract. `process_delivery_receipt` decodes this receipt but never reads `receipt.success` when deciding whether to pay the relayer. It settles (`register_reward`) purely based on `order.fee > 0` and removes the pending order unconditionally, regardless of whether delivery succeeded or reverted on Ethereum.

### Finding Description
The receipt type explicitly models delivery success: [1](#0-0) 

`submit_delivery_receipt` is a public, unprivileged, signed extrinsic. It verifies the raw Ethereum log/proof via the light-client verifier, decodes it into a `DeliveryReceipt`, and forwards it to settlement: [2](#0-1) 

Settlement itself only checks the gateway address and the pending-order nonce; it never inspects `receipt.success`: [3](#0-2) 

Because `success` is decoded but discarded, the settlement path advances (reward registered + `PendingOrders` entry removed) exactly the same way whether the Ethereum-side dispatch succeeded or failed. This violates the pivot requirement that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here decode succeeds but the encoded execution-success signal is never checked before payout state advances.

This is directly analogous to the `ArmadaTreasuryGov` bug class: the data model (spec-equivalent: the `success` field / event schema) declares an invariant that should gate a value-moving operation, but the corresponding enforcement code path for that gate was never implemented, and once the receipt is processed the `PendingOrders` entry is deleted — there is no retry or backfill path, i.e. the gap is permanent for that nonce (mirrors "contract is not upgradeable, so functionality can't be added later").

### Impact Explanation
Any relayer that submits a real, verifiable `InboundMessageDispatched` event for a message that failed on Ethereum (`success == false`, e.g. because it ran out of gas or reverted on the Gateway) still receives the full `order.fee` reward as if the message had succeeded. This is an incorrect/unconditional settlement: relayer rewards are paid without the delivery success guarantee the receipt schema itself encodes, and the affected `PendingOrders` entry is deleted immediately, so there is no mechanism to recover, re-attempt, or claw back the incorrect payout.

### Likelihood Explanation
The path is reachable by any signed account (public dispatchable, no admin/governance gate) as long as it can produce a light-client-verified event log for a nonce with `order.fee > 0` — this only requires a genuinely occurring but failed dispatch on the Gateway contract, which is a normal operational occurrence (out-of-gas, revert) rather than a compromised prover or colluding validator/relayer set. The verifier check only attests that the log is authentic, not that its `success` field is enforced downstream.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before registering the reward: pay the full/expected reward only when `success == true`; on `success == false`, either withhold the reward, pay a reduced/zero amount per protocol policy, or route the fee to a defined fallback destination (e.g., treasury or fee refund), and emit a distinct event for failed-delivery receipts so downstream indexers and governance can act on it deterministically.

### Proof of Concept
1. A message is enqueued via `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0`. [4](#0-3) 
2. The Gateway contract on Ethereum processes nonce `n` and the dispatch call fails (e.g. reverts), emitting `InboundMessageDispatched(nonce=n, topic, success=false, reward_address=R)`.
3. Any account submits `submit_delivery_receipt` with a valid Merkle/receipt proof of that real (failed) event; `T::Verifier::verify` succeeds because the log is genuine.
4. `DeliveryReceipt::try_from` decodes `success = false` correctly, but `process_delivery_receipt` never reads it: [5](#0-4) 
5. `T::RewardPayment::register_reward` credits `order.fee` to `R` exactly as it would for a successful delivery, and `PendingOrders::remove(nonce)` permanently deletes the record — no path exists afterward to correct the payout.

**Note on confidence**: I could not verify against `specs/GOVERNANCE.md`-equivalent Snowbridge specification documents (none were found in the index) whether reward-on-failure is explicitly disallowed by design intent versus being an accepted "pay for relaying attempt regardless of outcome" policy (analogous to EVM gas always being charged). The code-level fact — that `success` is decoded into the receipt struct but is unused in the only consumer (`process_delivery_receipt`) — is directly verified from the repository and is the basis of this finding.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-438)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
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
