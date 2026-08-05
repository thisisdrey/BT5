Audit Report

## Title
`process_delivery_receipt` pays the fixed relayer reward on any delivery receipt regardless of the on-chain `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` from a verified Ethereum event log but only checks `receipt.gateway` against `GatewayAddress` and that a `PendingOrders` entry exists for `receipt.nonce`; it never inspects `receipt.success` before unconditionally paying `order.fee` to the attacker-selectable `reward_account` via `T::RewardPayment::register_reward`. This allows an unprivileged relayer who obtains a genuine but failed-execution receipt from Ethereum (`success: false`) to still collect the full relayer reward as if the message had succeeded.

## Finding Description
`submit_delivery_receipt` is a public, unprivileged, signed extrinsic that verifies the Ethereum proof via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt`, and forwards it to `process_delivery_receipt`. [1](#0-0) 

Inside `process_delivery_receipt`, the pallet checks only the gateway address and pending-order existence, then pays `order.fee` unconditionally whenever it is greater than zero — the `success` field of the decoded receipt is never read or enforced anywhere in this function: [2](#0-1) 

The pallet's own doc comment states that reward payout is intended to happen only "When the message has been verified and executed," i.e., conditioned on successful Ethereum-side execution: [3](#0-2) 

The `PendingOrder` created in `do_process_message` carries `fee` unconditionally at message-send time, independent of eventual execution outcome: [4](#0-3) 

The existing guards (`Error::InvalidGateway`, `Error::InvalidPendingNonce`) only validate the gateway address and nonce existence — neither reviews the actual execution outcome carried in the receipt, so they are insufficient to prevent payout for failed executions.

## Impact Explanation
This constitutes an unbacked/incorrect payout from the bridge's relayer-reward pool: `order.fee` — a real balance amount tracked in `PendingOrder` — is settled to `reward_account` without the payout state ("message successfully delivered/executed") actually holding true. This matches the "duplicate settlement or payout" and "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" impact criteria, since here settlement advances after decode alone, without confirmed successful execution. An unprivileged relayer can repeatedly drain relayer-reward funds for messages that never executed correctly on Ethereum.

## Likelihood Explanation
The exploit path requires only a normal signed account submitting `submit_delivery_receipt` with a legitimately-verifiable Ethereum event log whose `success` field is `false` — no privileged access, governance action, or compromised peer/validator is needed. Any relayer who can cause (or observe) a failed execution on the Ethereum Gateway contract (e.g., by supplying insufficient gas or triggering a revert condition within their control) can collect a valid receipt log and claim the reward as if the message succeeded. Because the check is entirely absent (not merely weak), the issue is deterministic and repeatable for every failed-execution receipt.

## Recommendation
In `process_delivery_receipt`, gate the call to `T::RewardPayment::register_reward` on `receipt.success == true` in addition to `order.fee > 0`. For failed-execution receipts, remove the pending order to prevent state bloat but skip the reward payment, and emit a distinct event (e.g., `MessageDeliveryFailed`) rather than `MessageDelivered` so downstream consumers can distinguish successful settlement from failed execution.

## Proof of Concept
1. A relayer submits a message via the normal outbound flow; `do_process_message` inserts a `PendingOrder { nonce, fee, .. }` with `fee > 0`.
2. On Ethereum, the corresponding message execution fails/reverts (e.g., due to insufficient gas or another revert condition triggerable by the same relayer), and the Gateway contract emits a delivery-receipt event with `success = false`.
3. The relayer obtains the genuine event log and receipt-inclusion proof for this failed-execution event and calls `submit_delivery_receipt(event)`.
4. `T::Verifier::verify` succeeds (the event genuinely occurred and is included in a finalized Ethereum block); `DeliveryReceipt::try_from` decodes `success: false` along with `gateway`, `nonce`, and `reward_address`.
5. `process_delivery_receipt` checks only the gateway match and pending-order existence, then unconditionally calls `T::RewardPayment::register_reward(&reward_account, DefaultRewardKind, order.fee)`, paying the reward for a message that never successfully executed on Ethereum.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
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
