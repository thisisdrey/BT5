Based on my investigation, I found a strong local analog to the referral self-selection bug.

### Title
Reward payout redirected to attacker-controlled address via unbound `reward_address` field in delivery receipt - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The `InboundMessageDispatched` event emitted by the Ethereum Gateway contract carries a `reward_address` field that is decoded into `DeliveryReceipt::reward_address` and used directly as the account credited with the relayer reward, exactly mirroring the `referrer` parameter pattern in the reported `mintWithReferralFor()` bug: a value that determines "who gets paid" is taken from data associated with the transaction rather than being cryptographically bound to the actual party that performed the qualifying work (i.e. the specific `relayer` account that submitted `submit_delivery_receipt` and paid gas/verification costs on this chain).

### Finding Description
`process_delivery_receipt` derives the reward recipient like this: [1](#0-0) 

The `reward_address` originates from the `InboundMessageDispatched` Solidity event and is decoded straight from the event log without any check tying it to the caller/relayer of `submit_delivery_receipt`: [2](#0-1) 

The extrinsic accepts an arbitrary `relayer` (the signer) and an `event` whose `reward_address` is only checked for validity against the Beacon/receipt-proof pipeline, not against `relayer` identity: [3](#0-2) 

The `reward_address` value is set on the Ethereum side when the message is executed on the Gateway contract — i.e., whoever calls the Gateway's delivery function on Ethereum controls what `reward_address` gets embedded in the emitted event, not necessarily the party who eventually submits the Polkadot-side `submit_delivery_receipt` proof. Because Ethereum-side calls are permissionless (any address can call the Gateway to execute a queued inbound message), an attacker can:

1. Watch for a pending outbound order with a non-trivial `fee` (visible via `PendingOrders`).
2. Execute the corresponding message delivery on the Ethereum Gateway themselves (or via any unprivileged tx), setting `reward_address` to their own account instead of the account of the relayer who actually performed the off-chain relay work/monitoring.
3. Submit `submit_delivery_receipt` with the resulting event log/proof; verification passes because the proof is cryptographically valid — it just encodes an attacker-chosen `reward_address`.
4. `T::RewardPayment::register_reward` credits the attacker's account with `order.fee`, diverting the reward that was meant to compensate the relayer's cost of the confirmation flow.

This is structurally identical to the referral bug: the "beneficiary" (`reward_address` / `referrer`) is a free-form value embedded in attacker-influenced data rather than being derived from — or checked against — the entity whose qualifying action (the referral / the actual relay submission) is being rewarded.

### Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary" invariant for bridge reward payouts. An unprivileged actor can consistently capture `PendingOrders` fees intended for the parachain/relayer ecosystem that performs the actual delivery-confirmation service, degrading relayer incentives for Snowbridge message delivery and enabling systematic diversion of `fee`-denominated bridge rewards without any privileged access, malicious validator, or leaked key — only requiring the ability to call the permissionless Gateway contract on Ethereum.

### Likelihood Explanation
High. No special permissions are needed: `submit_delivery_receipt` is callable by any signed account, and executing/triggering delivery on the Ethereum Gateway is itself a public, permissionless action. Any actor monitoring `PendingOrders` for orders with `fee > 0` can front-run the legitimate relayer's reward claim by controlling the `reward_address` embedded at execution time.

### Recommendation
Bind the reward beneficiary to a value that cannot be freely chosen at message-execution time. Options: (a) fix the reward beneficiary at message send/order-creation time on the Polkadot side (analogous to storing `referrer` bound to `referralCode` in a mapping rather than accepting it as a free parameter), so the `PendingOrder` itself carries the authoritative beneficiary and the event-supplied `reward_address` is ignored or only used as an optional override that must be validated against a registered identity; or (b) require that `reward_address` matches (or is cryptographically tied to) the `relayer` who submits the delivery receipt, unless explicitly and safely delegated beforehand (similar to `claim_rewards_to`'s explicit alternate-beneficiary flow in `pallet_bridge_relayers`, which is a signed, self-directed override rather than an attacker-writable field from unrelated third-party data).

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0`. [4](#0-3) 
2. Attacker (not the honest relayer who tracked/would submit the receipt) executes the corresponding inbound message on the Ethereum Gateway contract, which emits `InboundMessageDispatched(nonce, topic, success, reward_address=<attacker_address>)`.
3. Attacker (or anyone) submits `submit_delivery_receipt` with a valid proof of this event.
4. `T::Verifier::verify` succeeds (proof is valid), `DeliveryReceipt::try_from` decodes `reward_address` as the attacker's address, and `process_delivery_receipt` credits `order.fee` to the attacker via `T::RewardPayment::register_reward`, as shown directly in the integration test pattern where `reward_address` is set independently of `relayer`: [5](#0-4) 

This test demonstrates the exact mechanic (a different account than `relayer` receiving the reward via `reward_address`), which is legitimate when `reward_address` is trusted, but becomes exploitable because nothing in the on-chain logic constrains who can set that value on the Ethereum side before the proof is generated.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-460)
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
```

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
