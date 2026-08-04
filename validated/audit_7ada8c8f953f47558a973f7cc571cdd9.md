Based on the evidence gathered, I have a concrete, self-contained analog within `bridges/snowbridge/pallets/outbound-queue-v2`.

### Title
Attacker-supplied `reward_address` in `DeliveryReceipt` lets any caller redirect Snowbridge relayer rewards to an arbitrary account - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` pays out the fee recorded in a `PendingOrder` to whatever account is encoded in the `reward_address` field of the externally-supplied `DeliveryReceipt`, with no binding between that field and the entity that actually did the relaying work that created the `PendingOrder`. The extrinsic that triggers this, `submit_delivery_receipt`, is permissionless (`ensure_signed`), so any account can submit a valid Merkle/beacon proof for an `InboundMessageDispatched` event and claim the reward for whichever `reward_address` is embedded in that event log.

### Finding Description
When an outbound message is committed in `do_process_message`, a `PendingOrder{ nonce, fee, block_number }` is stored — note this struct carries **no beneficiary/relayer identity**, only the `fee` amount: [1](#0-0) 

Later, any signed account can call `submit_delivery_receipt`, which only checks the Merkle/beacon proof of the event log and then defers entirely to `process_delivery_receipt`: [2](#0-1) 

Inside `process_delivery_receipt`, the beneficiary of the `fee` reward is taken directly from `receipt.reward_address` (falling back to the calling `relayer` only if that field is zero), and the `PendingOrder` is matched purely by `nonce` — there is no check that `reward_address` corresponds to whoever actually paid for/executed the delivery on the Ethereum side, nor to whoever committed/paid the original outbound fee: [3](#0-2) 

`reward_address` is decoded straight out of the `InboundMessageDispatched` Solidity event (`nonce`, `topic`, `success`, `reward_address`): [4](#0-3) 

The pallet's verification (`T::Verifier::verify`) only proves that *this exact log* was genuinely emitted on Ethereum for the given `nonce` — it never proves anything about who is entitled to be named in `reward_address`. This is structurally the same broken invariant as the Arbitrum bug: an address field carried inside an externally-authored payload is trusted by the settlement logic to control who receives value/privilege, without being cryptographically bound to the legitimate party that earned it. Existing guards (`GatewayAddress` check, proof verification, `InvalidPendingNonce` check) all validate *that a message was delivered*, but none validate *who should be paid* — that value is taken verbatim and unconditionally from attacker-influenceable input.

### Impact Explanation
This directly matches the "duplicate settlement or payout" / "wrong beneficiary" impact class: relayer rewards funded from the bridge/tokenomics treasury (via `T::RewardPayment::register_reward`) can be diverted to an account of the caller's choosing rather than the entity that performed and paid for the actual relay work, because the pallet never checks that `reward_address` is tied to the message's original committer or actual deliverer. Over repeated nonces this results in systematic reward misallocation funded by the bridge reward pool.

### Likelihood Explanation
`submit_delivery_receipt` requires only `ensure_signed` — no special role, no relayer registration, no governance/admin action. The only real barrier is producing a valid consensus proof for an on-chain Ethereum event, which is a normal, low-cost, permissionless part of the intended relayer flow (this is not "malicious relayer" abuse of a privileged role — it is any ordinary user exploiting the missing beneficiary binding). This makes the path readily exploitable by an unprivileged attacker with no elevated access.

### Recommendation
Bind the reward beneficiary to the message at commitment time rather than trusting a field freely embedded in the settlement-time proof: extend `PendingOrder` (or a companion map) to record the account authorized to claim the fee — e.g., the account that originally paid/committed the fee, or a nonce-bound "authorized relayer" set when the message enters the outbound queue — and validate `receipt.reward_address` against that recorded value in `process_delivery_receipt` before paying out, rejecting or falling back to the submitting `relayer` if no match is found.

### Proof of Concept
1. A legitimate outbound message is committed via `do_process_message`, creating `PendingOrders[nonce] = { fee: F, .. }`. [5](#0-4) 
2. The message is relayed to Ethereum and dispatched by the Gateway contract, which emits `InboundMessageDispatched(nonce, topic, success, reward_address)`.
3. An unrelated attacker, observing the finalized Ethereum block, constructs the beacon/Merkle proof for that event log (a public, permissionless action) and calls `submit_delivery_receipt` on BridgeHub with that proof, where `receipt.reward_address` is an account they control.
4. `process_delivery_receipt` accepts the proof, matches `PendingOrders[nonce]`, and pays fee `F` to the attacker's `reward_address` via `T::RewardPayment::register_reward`, exactly as shown in the test flow used throughout the emulated integration tests: [6](#0-5) 
5. The `PendingOrder` is removed, preventing the legitimate deliverer from ever claiming the reward for that nonce.

### Citations

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-426)
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

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { .. }) => {},
			]
		);
	});
```
