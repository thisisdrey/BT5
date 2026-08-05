## Title
`process_delivery_receipt` settles the reward for a `PendingOrder` by nonce alone, without binding the receipt to the specific committed message content - (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The external report describes a callback-confusion bug: `ClearingHouse`'s fallback trusts any inbound call as long as a stateful check (`collateralIdToAuction[collateralId]` is set) passes, without verifying that the call is genuinely tied to the specific auction/order it claims to settle. Anyone can supply an unrelated Seaport order whose consideration recipient is the `ClearingHouse`, and the fallback will settle the real, unrelated auction identified only by `collateralId`.

The closest structural analog in this repository is `Pallet::process_delivery_receipt` in `snowbridge-pallet-outbound-queue-v2`, which resolves and pays out a `PendingOrder` purely by looking up `PendingOrders::<T>::get(receipt.nonce)`, without cross-checking that the receipt's `topic`/message content corresponds to the specific `OutboundMessage` that was actually queued under that nonce. [1](#0-0) 

## Finding Description
`do_process_message` inserts a `PendingOrder { nonce, fee, block_number }` keyed only by `nonce` when a message is committed for delivery to Ethereum [2](#0-1) .

`submit_delivery_receipt` verifies the event log/proof cryptographically via `T::Verifier::verify`, decodes it into a `DeliveryReceipt`, and calls `process_delivery_receipt` [3](#0-2) .

`process_delivery_receipt` then:
1. Checks `receipt.gateway == T::GatewayAddress::get()`.
2. Selects `reward_account` from `receipt.reward_address` (attacker/relayer-controlled field embedded in the Ethereum event, not derived from the original message).
3. Fetches `PendingOrders::<T>::get(receipt.nonce)`.
4. Pays `order.fee` to `reward_account` and removes the order — with **no check that `receipt.topic` (or any other message-identifying field) matches the `topic`/content of the `OutboundMessage` that was actually stored under that `nonce`** in `Messages`/`MessageLeaves`. [1](#0-0) 

This mirrors the ClearingHouse pattern precisely: the settlement path trusts a single stateful lookup key (`collateralId` / `nonce`) to authorize the state-mutating payout, but never verifies that the *incoming call/event* is genuinely bound to the specific business object it claims to close out. In the Solidity bug, `collateralId` alone was reused to trigger a real auction's settlement from an unrelated Seaport order. Here, `nonce` alone is reused to resolve `PendingOrders`, and the payout beneficiary (`reward_address`) is taken directly from the submitted event rather than being cryptographically tied to the original outbound message or its intended recipient.

## Impact Explanation
If any code path on the Ethereum Gateway side can be induced to emit a `DeliveryReceipt`-shaped event with an attacker-chosen `nonce` that happens to coincide with a currently pending, unrelated order's nonce (e.g., through nonce collision across message batches, replay of stale/matured events, or a mismatch between the message that was actually executed and the nonce claimed in the receipt), the pallet will:
- Pay the `fee` to an arbitrary `reward_address` chosen by whoever crafted the event, not necessarily the account that performed genuine delivery work for that nonce.
- Prematurely remove the `PendingOrder`, permanently closing out the real pending settlement so a subsequent, legitimate delivery receipt for the same nonce is rejected (`Error::InvalidPendingNonce`), exactly analogous to the Solidity bug's "genuine auction settle reverts afterward" failure mode.

This is a fund-misdirection / duplicate-settlement class impact on the bridge's reward accounting, matching the "duplicate settlement or payout" and "permanent... bridge-state lock" categories in the impact gate.

## Likelihood Explanation
The likelihood is bounded by the fact that `T::Verifier::verify` cryptographically anchors the event log to a real Ethereum block via the light client, which is a much stronger binding than Seaport's arbitrary order construction. Exploiting this therefore requires a genuine, verifiable Ethereum-side event whose `nonce`/`topic` fields do not actually correspond 1:1 to the message dispatch it claims — this is plausible only if the Gateway contract (out of scope here) or the encoding/decoding of `DeliveryReceipt` allows `nonce` reuse or spoofable `reward_address`/`topic` independent of the actual dispatched command. Within this repository, the missing invariant is clear and verifiable by inspection: `process_delivery_receipt` never reads or checks `receipt.topic` against the stored `Messages`/`MessageLeaves` entry for that nonce, so the pallet-side guard that should prevent settlement-object confusion is absent.

## Recommendation
Bind the delivery receipt to the exact committed message, not just its nonce:
- Store the message's committed hash/topic (already computed in `do_process_message` as `message_abi_encoded_hash` / `id`) alongside the `PendingOrder`, and require `process_delivery_receipt` to verify `receipt.topic == order.topic` (or equivalent) before paying out and removing the order.
- Consider deriving `reward_account` deterministically from the original message's stored relayer/fee-payer context rather than trusting a `reward_address` field carried inside the externally-supplied receipt event.

## Proof of Concept
Conceptual reproduction path (bridge-side event fabrication is out of scope for a pure Substrate PoC, so this is expressed as the missing-check test case):
1. Insert `PendingOrder { nonce: 5, fee: 1_000_000, .. }` into `PendingOrders` (representing a genuine queued message with `topic = T1`).
2. Construct/verify a `DeliveryReceipt { gateway, nonce: 5, reward_address: attacker, topic: T2, success: true }` where `T2 != T1` (i.e., the event does not correspond to the message actually associated with nonce 5).
3. Call `OutboundQueue::submit_delivery_receipt` (or directly `process_delivery_receipt`) with this receipt.
4. Observe that the call succeeds: the `fee` is paid to `attacker`, and `PendingOrders::get(5)` is removed — even though the receipt's `topic` never matched the original message's identity, demonstrating the absence of any check binding the receipt to the specific message content [4](#0-3) .

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
