Confirmed: `GatewayAddress` is a single, compile-time (`#[pallet::constant]`) `H160` bound in the outbound-queue-v2 pallet, checked strictly in `process_delivery_receipt`. This is a direct structural analog to the CSVerifier `WITHDRAWAL_ADDRESS` issue. [1](#0-0) [2](#0-1) 

### Title
Single hardcoded `GatewayAddress` constant permanently strands relayer rewards and `PendingOrders` after an Ethereum Gateway contract upgrade - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`pallet-snowbridge-outbound-queue-v2` binds the Ethereum Gateway contract address to a single, compile-time runtime constant `T::GatewayAddress` [1](#0-0) , and `process_delivery_receipt` requires an exact match between this constant and the `gateway` field of every incoming `DeliveryReceipt` before it will register the relayer reward and remove the corresponding `PendingOrder` [3](#0-2) . This is structurally identical to the reported `CSVerifier._processWithdrawalProof` flaw: a single hardcoded reference address is used to validate proofs/receipts tied to a contract that is expected to be upgraded over the system's lifetime, with no mechanism to recognize a previous, still-valid address for in-flight state.

### Finding Description
Outbound messages queued for Ethereum are assigned a nonce and a `PendingOrder{ nonce, fee, block_number }` is stored while the message is in flight, waiting for a relayer to submit proof of on-chain delivery [4](#0-3) . The only way to settle that order — pay the relayer fee and clear the `PendingOrders` entry — is `process_delivery_receipt`, which unconditionally rejects the receipt with `Error::InvalidGateway` unless `receipt.gateway` equals the single compiled-in `T::GatewayAddress::get()` [5](#0-4) .

If the Ethereum-side Gateway contract is redeployed/upgraded to a new address (an expected lifecycle event for any upgradeable bridge contract, exactly the scenario the external report describes for the Lido withdrawal vault), any message that was already queued and committed against the *old* gateway, and any receipt legitimately emitted by that old gateway that has not yet been relayed, becomes permanently unprocessable the moment the runtime is updated to point `T::GatewayAddress` at the new address. There is no fallback list of historical addresses and no migration path for orphaned `PendingOrders` — the check is a single strict equality with no allowance for a prior valid value, mirroring the `WITHDRAWAL_ADDRESS == withdrawalAddress` check in the original report.

The identical pattern also gates inbound processing: `pallet::process_message` in `inbound-queue-v2` performs the same single-value strict check, `ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway)` [6](#0-5) , so inbound messages already emitted by an about-to-be-retired gateway are similarly rejected forever once the constant is updated, even though they were valid and paid for at emission time.

### Impact Explanation
The corrupted/stale value is the runtime constant `T::GatewayAddress` compared against `PendingOrder`/message state that was created against a previous, legitimately valid gateway address. Once the constant changes:
- Any `PendingOrder` fee tied to a stale gateway can never be paid out or cleared — this is a permanent fund lock of the escrowed relayer fee and unbounded storage growth in `PendingOrders`.
- Legitimate inbound messages already emitted (and paid for by the sender on Ethereum) from the old gateway are permanently rejected with `InvalidGateway`, denying users their bridged funds/XCM delivery with no recovery path in-pallet.
This falls squarely within the accepted impact class of "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" avoidance failing into "no settlement ever" for legitimately-issued state.

### Likelihood Explanation
This is not contingent on any malicious peer, relayer, or governance abuse — it is triggered by the ordinary, expected operational event of upgrading the Ethereum Gateway contract, which the bridge's own architecture anticipates (the constant is explicitly a configurable, upgradeable parameter rather than an immutable protocol invariant). Any in-flight message or receipt that straddles a gateway migration window is affected automatically, without any attacker action required, making the likelihood of triggering the stuck state effectively certain over the bridge's operational lifetime whenever the Gateway is redeployed.

### Recommendation
Track a bounded set (or versioned history keyed by activation block/nonce range) of previously valid Gateway addresses rather than a single constant, and accept receipts/messages that match any address that was valid over the corresponding message's queuing period. Alternatively, add an explicit migration step that drains/settles all `PendingOrders` (and rejects/finalizes in-flight inbound nonces) before `GatewayAddress` is changed, so no state can reference a gateway address the pallet no longer recognizes.

### Proof of Concept
1. Deploy/queue an outbound message via `send_message_impl`, producing `PendingOrders::<T>::insert(nonce, PendingOrder{ nonce, fee, block_number })` with `fee > 0`.
2. Before a relayer submits `submit_delivery_receipt` for that nonce, perform a runtime upgrade that changes the value bound to `T::GatewayAddress` (simulating the Ethereum Gateway contract being redeployed).
3. Relayer now submits a valid `DeliveryReceipt` with `gateway` set to the *old* address (the one the original message was actually sent to/from) — `process_delivery_receipt` compares `T::GatewayAddress::get()` (now the *new* address) against `receipt.gateway` (the *old* address) at [5](#0-4)  and returns `Error::<T>::InvalidGateway`.
4. The relayer fee is never paid, and the `PendingOrder` for that nonce remains in storage indefinitely — there is no extrinsic or code path to clear or re-route it, confirming the permanent lock.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L21-41)
```rust
//! 5. The message is processed in `Pallet::do_process_message`:
//! 	a. Convert to `OutboundMessage`, and stored into the `Messages` vector storage
//! 	b. ABI-encode the `OutboundMessage` and store the committed Keccak256 hash in `MessageLeaves`
//! 	c. Generate `PendingOrder` with assigned nonce and fee attached, stored into the
//! 	   `PendingOrders` map storage, with nonce as the key
//! 	d. Increment nonce and update the `Nonce` storage
//! 6. At the end of the block, a merkle root is constructed from all the leaves in `MessageLeaves`.
//!    At the beginning of the next block, both `Messages` and `MessageLeaves` are dropped so that
//!    state at each block only holds the messages processed in that block.
//! 7. This merkle root is inserted into the parachain header as a digest item
//! 8. Offchain relayers are able to relay the message to Ethereum after:
//! 	a. Generating a merkle proof for the committed message using the `prove_message` runtime API
//! 	b. Reading the actual message content from the `Messages` vector in storage
//! 9. On the Ethereum side, the message root is ultimately the thing being verified by the Beefy
//!    light client.
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L161-163)
```rust
		/// Address of the Gateway contract
		#[pallet::constant]
		type GatewayAddress: Get<H160>;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-475)
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
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L216-217)
```rust
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);
```
