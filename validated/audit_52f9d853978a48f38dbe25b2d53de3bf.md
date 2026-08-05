Confirmed via `grep_search` that `receipt.success` is only ever constructed (never read/checked) anywhere in the `bridges/snowbridge` tree — no code path consults it before paying the reward.

Audit Report

## Title
Relayer reward paid on failed Ethereum message delivery because `DeliveryReceipt.success` is never checked - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` pays the relayer reward for a `PendingOrder` based solely on `order.fee > 0`, without ever inspecting the `success` field of the decoded `DeliveryReceipt`. As a result, a message whose execution on Ethereum genuinely failed (`success: false`) is rewarded identically to a successful delivery.

## Finding Description
The Gateway contract's `InboundMessageDispatched(nonce, topic, success, reward_address)` event explicitly signals delivery outcome via `success`, and this is faithfully decoded into `DeliveryReceipt::success` [1](#0-0) . `submit_delivery_receipt` is a public, unprivileged signed extrinsic that verifies the proof, decodes the receipt, and forwards it to `process_delivery_receipt` [2](#0-1) .

Inside `process_delivery_receipt`, the only checks performed are that the gateway address matches (`ensure!(T::GatewayAddress::get() == receipt.gateway, ...)`) and that a `PendingOrder` still exists for the nonce; the payout branch at `if order.fee > 0 { ... register_reward ... }` never reads `receipt.success` before calling `T::RewardPayment::register_reward`, and the order is then unconditionally removed [3](#0-2) . Neither the gateway-address check nor the `PendingOrders` existence check constrains the outcome of delivery — both are satisfied identically whether `success` is `true` or `false`. A `grep_search` across the entire `bridges/snowbridge` tree confirms `receipt.success` is set only at decode time and is never read anywhere else in the codebase, so it is functionally dead data.

## Impact Explanation
This is an unbacked/duplicate-style payout: an attacker (any relaying party, without privilege) can submit a real, verifiably authentic `InboundMessageDispatched` event where `success == false` and still cause `T::RewardPayment::register_reward` to credit the full `order.fee` to the reward account, while the corrupted/ignored value is the payout-gating decision at line 466 which should have consulted `receipt.success` but only checks `order.fee > 0`. This matches the "duplicate settlement or payout" and "payout state must only advance after ... execution ... succeed" impact classes, draining the bridge reward pool without a corresponding successful cross-chain effect.

## Likelihood Explanation
The exploit path requires nothing beyond an unprivileged, signed call to `submit_delivery_receipt` with a legitimate, chain-verifiable proof of a real `InboundMessageDispatched` event — no malicious peer, prover, or governance assumption is needed, since failed dispatches on Ethereum (out-of-gas, reverted commands) are an expected, ordinary occurrence in cross-chain messaging, making this readily triggerable in production.

## Recommendation
Gate the reward payment on `receipt.success` in `process_delivery_receipt`, e.g. change `if order.fee > 0 { ... }` to `if order.fee > 0 && receipt.success { ... }`, and emit a distinct event (e.g. `MessageDeliveryFailed`) when `success == false` so failed deliveries are observable without being rewarded.

## Proof of Concept
1. `do_process_message` inserts a `PendingOrder { nonce, fee: F, .. }` with `F > 0` into `PendingOrders` [4](#0-3) .
2. On Ethereum the Gateway processes the message but dispatch fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid proof for this real event and calls `submit_delivery_receipt(origin, event)`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly.
4. `process_delivery_receipt` still executes `if order.fee > 0 { register_reward(...) }`, paying the fee in full and emitting `MessageDelivered`, identical to the success path — confirmable by modifying any existing test (e.g. `submit_delivery_receipt_succeeds_after_unhalt`) to construct a `success: false` event and observing the reward is still registered.

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
