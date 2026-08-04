### Title
Relayer reward paid regardless of delivery outcome — unchecked `success` field in `process_delivery_receipt` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core invariant break is: a value recorded at message-open time (`amountsPerNonce`) is later consumed/settled without validating it against the actual outcome of the corresponding operation, allowing a mismatch between what was recorded and what actually happened. The local analog is in `pallet-outbound-queue-v2`'s delivery-receipt settlement path: a `PendingOrder` (fee owed) is created when a message is queued for Ethereum, and later fully paid out from `process_delivery_receipt` purely based on `nonce` existing in `PendingOrders`, while the decoded `DeliveryReceipt.success` flag — which reflects whether the message actually executed successfully on Ethereum — is never checked before paying the reward.

### Finding Description
`DeliveryReceipt` is decoded from the Ethereum `InboundMessageDispatched` event and explicitly carries a `success: bool` field indicating whether the message dispatch succeeded on Ethereum: [1](#0-0) 

However, `Pallet::process_delivery_receipt` only checks the gateway address and the existence of a `PendingOrder` for the given `nonce` — it never reads or validates `receipt.success` before paying the reward and removing the order: [2](#0-1) 

The `PendingOrder` itself only stores `nonce`, `block_number`, and `fee` — it has no field to later cross-check against the actual execution outcome: [3](#0-2) 

This mirrors the reported bug class exactly: a record generated at one step (`preRelease`/`amountsPerNonce`, here `do_process_message`/`PendingOrder`) is consumed at another step (`depositToClose`, here `submit_delivery_receipt`) without validating the record against the actual state/outcome of the operation it is supposed to represent. In the original report, the missing validation is nonce-usage/amount reconciliation; here it is success/outcome reconciliation — the settlement path trusts that "nonce exists in `PendingOrders`" is sufficient for payout, ignoring the very field (`success`) that was added to the receipt precisely to convey whether the corresponding operation actually completed correctly.

### Impact Explanation
Any relayer can call the public, unprivileged `submit_delivery_receipt` extrinsic with a validly-verified Ethereum proof for a message whose dispatch failed on the Ethereum side (`success: false` — e.g., reverted commands, out-of-gas, or malformed payload execution) and still receive the full relayer fee recorded in `PendingOrders`, exactly as if delivery had succeeded. This is a fund-accounting/payout-correctness bug: value (`order.fee`) is settled to the relayer regardless of whether the bridge actually performed the promised work, and the `PendingOrders` entry is removed either way, making outcome-based reconciliation offchain the only remaining line of defense — the same underlying weakness (offchain-only reconciliation as compensating control) that the external report calls out as insufficient and explicitly recommends fixing onchain.

### Likelihood Explanation
Likelihood is high because:
- `submit_delivery_receipt` is a public, unprivileged extrinsic callable by any relayer.
- Ethereum-side command execution failures (reverts) are a normal, expected occurrence (gas griefing, malformed commands, target contract reverts), not an attacker-only scenario.
- No additional privilege, malicious peer, or governance actor is required — a relayer simply submits a legitimate, verifier-accepted proof for a message whose `success` flag happens to be `false`.

### Recommendation
In `process_delivery_receipt`, check `receipt.success` before crediting `order.fee` to the reward account. If `success` is `false`, either withhold/reduce the reward, or route it through a separate handling path (e.g., partial gas-cost-only reward) rather than paying the full fee as if delivery succeeded. Emit distinct events for successful vs. failed deliveries so downstream consumers (and audits) can distinguish the two outcomes on-chain rather than relying on offchain monitoring.

### Proof of Concept
1. A message is queued and processed by `do_process_message`, creating `PendingOrders[nonce] = PendingOrder { nonce, fee, block_number }` (see `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:426-438`).
2. On Ethereum, the corresponding Gateway command execution reverts/fails, so the real `InboundMessageDispatched` event is emitted with `success = false`.
3. A relayer obtains a valid receipt/execution proof for this failed-dispatch event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the proof is legitimate), `DeliveryReceipt::try_from` decodes `success = false` correctly, but `process_delivery_receipt` never inspects it: [4](#0-3) 
5. `order.fee` is paid in full via `T::RewardPayment::register_reward`, and `PendingOrders::remove(nonce)` is executed — the relayer is fully rewarded for a delivery that did not actually succeed.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs (L14-24)
```rust
/// Pending order
#[derive(Encode, Decode, TypeInfo, Clone, Eq, PartialEq, Debug, MaxEncodedLen)]
pub struct PendingOrder<BlockNumber> {
	/// The nonce used to identify the message
	pub nonce: u64,
	/// The block number in which the message was committed
	pub block_number: BlockNumber,
	/// The fee in Ether provided by the user to incentivize message delivery
	#[codec(compact)]
	pub fee: u128,
}
```
