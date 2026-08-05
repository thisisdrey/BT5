### Title
`snowbridge-pallet-outbound-queue-v2::process_delivery_receipt` pays relayer reward and settles `PendingOrder` without checking the `success` field of the delivery receipt - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The Snowbridge V2 outbound queue's `process_delivery_receipt` function is the direct local analog of the `Gateway.sol` bug class: a payout/settlement action that fires based on an accounting record (`PendingOrder`) without validating that the underlying delivery actually completed as intended. In the Solidity report, `receiveQuery()` blindly assumes `nativeTokenAmount` still reflects the pending state; here, `process_delivery_receipt` blindly pays the relayer reward and irreversibly removes the `PendingOrder` on any verified delivery receipt for a known nonce — regardless of whether the receipt reports `success: true` or `success: false`.

### Finding Description
`Pallet::<T>::submit_delivery_receipt` verifies the Merkle/event proof for an Ethereum event log and decodes it into a `DeliveryReceipt`, then calls `Self::process_delivery_receipt(relayer, receipt)`: [1](#0-0) 

The receipt-processing logic only checks the gateway address and whether the nonce has a matching `PendingOrder`; it never inspects `receipt.success`: [2](#0-1) 

The `DeliveryReceipt` type (decoded from the real, verified Ethereum `InboundMessageDispatched`-style event) is defined in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` and carries a `success` field indicating whether the message actually executed successfully on the Ethereum Gateway contract. The pallet fetches the `PendingOrder` (which holds the relayer fee), pays out `order.fee` via `T::RewardPayment::register_reward(...)` whenever `order.fee > 0`, and then unconditionally removes the order from `PendingOrders`, regardless of the `success` flag.

This breaks the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A delivery receipt reporting `success: false` (i.e., the message reverted/failed to execute on the Ethereum Gateway) is treated identically to a successful delivery: the relayer is still rewarded and the pending-order bookkeeping is deleted forever, with no retry path and no distinct handling for failed deliveries.

The existing test suite even has an explicitly named regression check for a related concern (`poc_m1`) that reward payment must be blocked when the *verifier* reports the bridge halted: [3](#0-2) 
— but this only covers the halted-verifier case, not the semantically distinct and unguarded `receipt.success == false` case, which the production code path does not gate on at all.

### Impact Explanation
This is a public-underpriced-work / incorrect-settlement bug with direct financial and bridge-liveness impact:
- Any permissionless relayer can submit a delivery receipt for a message that failed on Ethereum (a legitimately verifiable proof, no forged data required — the failure is real, `success: false` is honestly reported by the chain) and still collect the full relayer reward intended only for successful delivery.
- The `PendingOrder` record is deleted upon receiving *any* verified receipt, successful or not, so there is no mechanism left to re-attempt or re-reward genuine delivery of that message; the message's economic incentive is permanently consumed even though the corresponding action was never actually completed on the destination chain.
- This directly violates the required invariant that payout state must only advance after "execution ... succeed atomically," and constitutes underpriced/duplicate-settlement of relayer work without a corresponding beneficial delivery, which can be repeated across many messages to drain reward funds and degrade the economic security of bridge message delivery.

### Likelihood Explanation
High likelihood: `submit_delivery_receipt` is a public, unprivileged extrinsic that only requires `ensure_signed`, and the only work needed by an attacker/relayer is to obtain a legitimate Ethereum event log with `success: false` (e.g., by intentionally submitting a message whose execution predictably reverts on the Gateway, or simply capturing any organically occurring failed delivery) and relaying that proof. No admin, governance, or malicious-validator assumption is required — this is a standard permissionless relayer action exploiting a missing state check in the pallet itself.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- On `success == true`, keep current behavior (pay reward, remove `PendingOrder`).
- On `success == false`, do not pay the reward from this path; either leave the `PendingOrder` in place for a legitimate retry/settlement mechanism, or route it to a distinct "failed delivery" handling path (e.g., emit a `MessageDeliveryFailed` event and apply a separate, explicit refund/slash/retry policy) so unsuccessful execution can never trigger payout or unrecoverable removal of pending accounting state.

### Proof of Concept
1. A message is queued via the outbound-queue-v2 pipeline, creating `PendingOrders::<T>::insert(nonce, PendingOrder { nonce, fee, block_number })` with `fee > 0` (as shown in the message-commit path at lines 426-436 of `lib.rs`).
2. The message is delivered to the Ethereum Gateway but its Ethereum-side execution reverts/fails; the Gateway still emits a delivery event with `success: false` (this is a real, honestly-generated Ethereum event, not a forged one).
3. Any relayer calls `submit_delivery_receipt` with the valid Merkle/event proof for this failed-execution event.
4. `T::Verifier::verify` succeeds (the proof is legitimate), `DeliveryReceipt::try_from` decodes `success: false`, but `process_delivery_receipt` (lines 446-480) never inspects `receipt.success`; it pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward` and calls `<PendingOrders<T>>::remove(nonce)`.
5. The relayer has now claimed a reward for a message that was never successfully delivered/executed, and the corresponding `PendingOrder` can never be settled or retried again — reproducing the same "state advances/payout fires despite the operation's real-world failure" class of bug described in the external `Gateway.sol` report.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-416)
```rust
// Reward processing must be blocked while the bridge is halted: `submit_delivery_receipt`
// should not pay out `PendingOrder` fees if the verifier reports the bridge as halted.
#[test]
fn poc_m1() {
	new_tester().execute_with(|| {
		let nonce = 1;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_event_proof());

		set_verifier_halted(true);

		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);

		let order_after = PendingOrders::<Test>::get(nonce).expect("order still present");
		assert_eq!(order_after.fee, fee);

		set_verifier_halted(false);
	});
}
```
