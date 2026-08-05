### Title
`process_delivery_receipt` pays the fixed relayer reward on any delivery receipt regardless of the on-chain `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is that a function decides which check/branch to apply using a hardcoded parameter instead of a value that actually reflects the real transaction direction/outcome, causing an incorrect (but here, permissive rather than restrictive) result. The direct analog in this repository is `Pallet::process_delivery_receipt` in the Snowbridge V2 outbound queue: it decodes a `DeliveryReceipt` (which contains a `success` field, per the integration tests) but never inspects that field before releasing the relayer reward for `order.fee`.

### Finding Description
`submit_delivery_receipt` is a public, unprivileged, signed extrinsic [1](#0-0) . It verifies the Ethereum proof via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt`, and forwards it to `process_delivery_receipt` [2](#0-1) .

Inside `process_delivery_receipt`, the pallet only checks:
- that `receipt.gateway` matches the configured `GatewayAddress`,
- that a `PendingOrders` entry exists for `receipt.nonce`,

then unconditionally pays `order.fee` to the (attacker-selectable) `reward_account` whenever `order.fee > 0` [3](#0-2) . The `success` field that is part of the `DeliveryReceipt` structure (visible from the emulated test constructing a receipt with `success: true`) [4](#0-3)  is never read or enforced in `process_delivery_receipt`. The pallet's own doc comment states the intended design is "Fetch the pending order by nonce of the message, pay reward with fee attached in the order" only "When the message has been verified and executed" [5](#0-4) , i.e. the payout is meant to be conditioned on successful execution on Ethereum, exactly analogous to the external report's expectation that a check parameter (native-token flag / correct token address) must reflect the real state of the transfer rather than a constant.

Because `success` is not gated, any relayer can submit a valid Ethereum receipt log (one that correctly passes header/receipt-inclusion verification, i.e. the event genuinely happened on Ethereum) reporting a failed message execution (`success: false`) and still be paid the full `order.fee`, exactly as if the message had succeeded. This mirrors the external bug class: a verification/decision function omits (or hardcodes past) the one parameter that should vary with the real-world outcome of the operation, so the check result no longer reflects reality.

### Impact Explanation
This directly falls under "duplicate settlement or payout" / "message queues... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." An unprivileged relayer (no admin, no governance, no malicious validator/collator needed - just a normal signed account able to submit a genuine but failed-execution receipt from Ethereum) can drain relayer-reward funds for messages that never actually executed on the destination chain, since `order.fee` is paid unconditionally once a matching pending order and gateway address are found. This is an unbacked/incorrect payout from the bridge's reward pool paid to an attacker-chosen `reward_account` (`receipt.reward_address`), constituting theft of bridge funds without needing any exploited peer, node, or governance actor.

### Likelihood Explanation
Likelihood is high: the path is a public extrinsic (`submit_delivery_receipt`) reachable by any signed account, requires only a legitimately-verifiable Ethereum log (which an attacker who controls the corresponding Ethereum-side call can produce with `success = false`), and the code path that pays the reward performs no check on `receipt.success` at all. No race condition, no privileged access, and no additional guard elsewhere in the pallet (`Error::RewardPaymentFailed` exists in the `Error` enum but is unused/never triggered from this function) prevents this.

### Recommendation
In `process_delivery_receipt`, only call `T::RewardPayment::register_reward` when `receipt.success` is `true` (in addition to `order.fee > 0`). If a failed-execution receipt should still clear the pending order (to avoid indefinite growth of `PendingOrders`), remove the order but skip/adjust the reward payment, and consider emitting a distinct event (e.g. `MessageDeliveryFailed`) instead of `MessageDelivered` so downstream consumers can distinguish successful settlement from failed execution.

### Proof of Concept
1. A relayer submits a message via the normal outbound flow; `do_process_message` creates a `PendingOrder { nonce, fee, .. }` with `fee > 0` [6](#0-5) .
2. On Ethereum, the corresponding message execution reverts/fails (e.g., due to insufficient gas supplied by the same relayer, or any other on-chain revert condition within attacker control), and the Gateway contract emits a delivery-receipt event with `success = false`.
3. The relayer collects the genuine event log + Merkle/receipt proof for this failed-execution event and calls `submit_delivery_receipt(event)`.
4. `T::Verifier::verify` succeeds (the event genuinely occurred and is correctly included in a finalized Ethereum block), `DeliveryReceipt::try_from` decodes `success: false` along with `gateway`, `nonce`, `reward_address`.
5. `process_delivery_receipt` checks only `gateway` match and pending-order existence, then unconditionally executes `T::RewardPayment::register_reward(&reward_account, DefaultRewardKind, order.fee)` regardless of the decoded `success` value [7](#0-6) , paying out the reward for a message that never successfully executed.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L453-473)
```rust
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
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L409-415)
```rust
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};
```
