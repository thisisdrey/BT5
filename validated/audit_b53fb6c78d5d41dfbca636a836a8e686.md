### Title
Relayer reward is paid unconditionally on `submit_delivery_receipt` without checking the delivery `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`OutboundQueue::process_delivery_receipt` pays the full pre-committed `PendingOrder.fee` to the relayer and clears the pending order purely on the basis of a valid Ethereum event-log proof for a given `nonce` and `gateway`, without ever inspecting the `success` field carried by the `DeliveryReceipt`. This mirrors the LIDO bug's core flaw: the protocol assumes that "message accepted for processing" (fee reserved at enqueue time) always equals "message successfully executed" (the condition that should gate settlement), and it settles based on the former rather than verifying the latter.

### Finding Description
When a message is enqueued, `do_process_message` stores a `PendingOrder { nonce, fee, block_number }` keyed by nonce, where `fee` is the amount the user set aside to pay for delivery: [1](#0-0) 

When a relayer later calls `submit_delivery_receipt` with proof of an Ethereum event, the decoded `DeliveryReceipt` is passed to `process_delivery_receipt`: [2](#0-1) 

The function checks only `receipt.gateway` (against `T::GatewayAddress`) and resolves `reward_account`/`nonce`, then unconditionally does:
```rust
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
```
`receipt.success` — which the tests show exists on the `DeliveryReceipt` type and is populated from the Ethereum `InboundMessageDispatched`-style event (`success: true`/`success: true`/`success: true` seen throughout the emulated tests) — is never read in this settlement path: [3](#0-2) 

The pallet's own module docs describe the intended flow as "Fetch the pending order by nonce ... pay reward with fee attached in the order ... Remove the order," with no mention of checking whether the commands on the Ethereum side actually executed successfully: [4](#0-3) 

This is the direct analog of the LIDO bug: `totalAssets()`/reward settlement is computed from the *requested*/committed value (`order.fee`, fixed when the message was queued) rather than the *actual outcome* (whether the commands succeeded on Ethereum). The existing guard — the halted-verifier check demonstrated in `poc_m1` — only blocks payout while the bridge is globally halted; it does not gate payout on per-message execution success: [5](#0-4) 

### Impact Explanation
Because settlement (fee payout + `PendingOrders` removal) advances purely on proof-of-inclusion of an event log, without verifying `receipt.success`, a message whose commands revert or fail on the Ethereum side (out-of-gas, reverted token transfer, malformed command execution, etc.) still results in the relayer being paid the full fee and the order being permanently cleared. This is exactly the class of bug the required impacts describe: "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Here the payout advances even when execution did not succeed, which is a duplicate/unwarranted payout of value that was reserved by the user to guarantee real work, and it removes the `PendingOrder` so there is no path to re-attempt or refund it — a form of public underpriced/free settlement that degrades the economic guarantees of the bridge's fee model, analogous to the vault crediting itself for ETH it never actually received.

### Likelihood Explanation
The path is reachable by any unprivileged account: `submit_delivery_receipt` is a public, permissionless extrinsic that only requires a valid state/receipt proof for an event that genuinely occurred on Ethereum (no malicious relayer, prover, or governance actor is required — the underlying Ethereum transaction can legitimately revert due to gas limits, target contract behavior, or benign execution failure, and a normal, honest relayer would still submit the receipt to be compensated). Likelihood is low-to-medium (depends on how often Ethereum-side execution actually fails for otherwise valid nonces/gateway matches), but the check that is missing (`receipt.success`) is present as a field yet structurally unused in the payout function, indicating the intended invariant was not enforced.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `success == true`, proceed with the existing reward payment and remove the `PendingOrder` as today.
- If `success == false`, do not pay the relayer the full committed fee for having "delivered" a failing message; either pay only a smaller delivery-only portion (if the pallet's fee model separates delivery cost from execution reward), refund/reclaim the fee to the user, or otherwise settle deterministically instead of unconditionally paying `order.fee`. Add explicit unit tests asserting no (or reduced) reward is registered when `receipt.success == false`, mirroring the existing halted-verifier tests (`poc_m1`, `submit_delivery_receipt_succeeds_after_unhalt`).

### Proof of Concept
1. A user submits an XCM/Ethereum-bound message via the outbound queue v2 pipeline; `do_process_message` stores `PendingOrders[nonce] = { fee: F, ... }`.
2. On Ethereum, the Gateway processes the message but the command execution reverts/fails (e.g., an ERC-20 transfer inside the command fails), while the Gateway still emits a valid `InboundMessageDispatched`-style log with `success = false` for `nonce`.
3. A relayer (or the message's own origin, self-relaying) submits `submit_delivery_receipt(origin, event_proof)`.
4. `process_delivery_receipt` is invoked with `receipt.success == false`; it never inspects that field, checks only `receipt.gateway == T::GatewayAddress` (true) and `PendingOrders::get(nonce)` (present), then executes `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and removes the `PendingOrder`.
5. Result: the relayer/reward account is paid `F` even though the message's on-chain effects failed, and the `PendingOrder` is gone, precluding any retry/refund — confirmed directly by the code path at [6](#0-5)  which contains no reference to `receipt.success`.

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L103-115)
```rust
		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));

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
