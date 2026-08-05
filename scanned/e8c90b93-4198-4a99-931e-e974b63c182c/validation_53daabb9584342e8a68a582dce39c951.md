### Title
Outbound Queue V2 accepts zero/near-zero relayer fee messages, enabling underpriced spam of the Snowbridge outbound pipeline - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is: a value that is supposed to price/compensate for real work (LST redemption risk) can be pushed below the real cost, so the entity performing the work (Trove holders / arbitrageurs) either absorbs a loss or the work becomes free/underpriced, opening it to abuse. The Polkadot-SDK analog is in Snowbridge's V2 outbound pipeline: unlike V1, which enforces `PricingParameters::validate()` requiring a strictly non-zero relayer reward, V2's `SendMessage::validate` and `do_process_message` accept any `fee` value — including `0` — with no minimum-fee check, contradicting the pipeline's own design intent documented in `bridges/snowbridge/docs/v2.md`.

### Finding Description
In V1, pricing parameters are validated on-chain and must include a non-zero reward: [1](#0-0) 

In V2, the fee attached to an outbound message is instead supplied entirely by the sender/off-chain estimator and embedded in the queued `Message`. `SendMessage::validate` for the V2 outbound queue only checks payload size, never checks that `fee` (or its remote reward component) is non-zero or above any minimum: [2](#0-1) 

`do_process_message`, which is the entry point invoked by `MessageQueue` for every enqueued message, decodes the `Message` (including its `fee` field), converts it into an `OutboundMessage`, appends it to `MessageLeaves` (to be merklized and committed into the header digest at `on_finalize`/`commit`), and creates a `PendingOrder` carrying whatever `fee` was supplied — again with no minimum check: [3](#0-2) 

The reward is only paid out later, when/if a relayer submits a delivery receipt: [4](#0-3) 

The design documentation for V2 explicitly acknowledges this exact risk and states that a minimum relayer reward equal to the existential deposit should be imposed specifically "to stop spamming messages with 0 rewards": [5](#0-4) 

However, no such minimum-fee/minimum-reward enforcement exists in `do_process_message`, `PendingOrder` construction, or the V2 `XcmConverter`/`EthereumBlobExporter` validation path (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`), which only checks that the fee asset is Ether/WETH, not that its amount is meaningful.

### Impact Explanation
Because any signed/authorized origin able to route an XCM through `ExportMessage` to Ethereum (e.g., any parachain via Asset Hub → Bridge Hub) can attach an arbitrarily small (even zero) `PayFees` amount, an attacker can:
- Enqueue many messages that consume `MaxMessagesPerBlock` capacity, `MessageLeaves`/`Messages` storage, weight for `do_process_message`, and `commit()` merkle-root construction on Bridge Hub — all real chain resources — for negligible or zero cost.
- Populate `PendingOrders` with entries no rational relayer will ever deliver (since the reward doesn't cover gas), causing these orders to remain stuck in storage indefinitely (they are only removed via `submit_delivery_receipt`, which no relayer will submit for an unprofitable message).
- Crowd out legitimately-priced messages competing for the same fixed per-block message-processing budget (`MaxMessagesPerBlock`), degrading throughput of Bridge Hub message processing and stalling genuine bridge delivery — matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

This is directly analogous to the report's core issue: a fee/pricing value intended to cover real cost/risk can be driven below the threshold needed to make the work economically rational, and the system has no on-chain floor to prevent it, unlike the equivalent V1 code path which does enforce a floor.

### Likelihood Explanation
High likelihood of triggerability: no privileged actor is required. Any parachain (or any account with XCM send rights reaching the Bridge Hub `ExportMessage` path) can submit a message with `PayFees(WETH, epsilon)` and it will be accepted, queued, hashed into the committed merkle tree, and stored as a `PendingOrder`, exactly as documented as a known risk in the V2 design doc but seemingly not mitigated in the implementation reviewed.

### Recommendation
Enforce a minimum fee/reward (e.g., an existential-deposit-equivalent floor in WETH, as the design doc specifies) either in the V2 `XcmConverter`/`EthereumBlobExporter::validate` when parsing `PayFees`, or in `SendMessage::validate`/`do_process_message` in `outbound-queue-v2`, rejecting messages whose fee is below the floor — mirroring the non-zero-reward invariant already enforced by `PricingParameters::validate` in V1.

### Proof of Concept
1. Construct an XCM matching the V2 exporter's expected pattern:
```
WithdrawAsset(ETH, 1)      // minimal amount
PayFees(ETH, 1)            // fee = 1 wei
WithdrawAsset(ENA, amount) // or ReserveAssetDeposited(PNA, amount)
AliasOrigin(origin)
DepositAsset(...)
SetTopic(topic)
```
2. Route it via `pallet_xcm::execute`/`send` from any authorized origin through Asset Hub to Bridge Hub's `ExportMessage`.
3. `EthereumBlobExporter::validate` (V2) accepts it because it only validates asset kind (Ether) and instruction shape, not fee magnitude — see `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs` (fee-asset check only) and `send_message_impl.rs` (size-only check).
4. `do_process_message` in `outbound-queue-v2/src/lib.rs` enqueues the message, appends its hash to `MessageLeaves`, and stores a `PendingOrder{ fee: 1, ... }`.
5. Repeat N times per block up to `MaxMessagesPerBlock`; no relayer will ever call `submit_delivery_receipt` for these (uneconomical), so they remain permanently in `PendingOrders`, while committing weight/PoV/storage each block and crowding out legitimately-fee-paying messages.

### Citations

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L39-56)
```rust
	pub fn validate(&self) -> Result<(), InvalidPricingParameters> {
		if self.exchange_rate == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.fee_per_gas == U256::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.local.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.remote.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.multiplier == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		Ok(())
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-32)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		Ok(message.clone())
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L360-443)
```rust
			// Decode bytes into Message
			let Message { origin, id, fee, commands } =
				Message::decode(&mut message).map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: None,
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?;

			// Convert it to OutboundMessage and save into Messages storage
			let commands: Vec<OutboundCommandWrapper> = commands
				.into_iter()
				.map(|command| OutboundCommandWrapper {
					kind: command.index(),
					gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command),
					payload: command.abi_encode(),
				})
				.collect();

			let nonce = <Nonce<T>>::get().checked_add(1).ok_or_else(|| {
				Self::deposit_event(Event::MessageRejected {
					id: None,
					payload: message.to_vec(),
					error: Unsupported,
				});
				Unsupported
			})?;

			let outbound_message = OutboundMessage {
				origin,
				nonce,
				topic: id,
				commands: commands.clone().try_into().map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: Some(id),
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?,
			};
			Messages::<T>::append(outbound_message);

			// Convert it to an OutboundMessageWrapper (in ABI format), hash it using Keccak256 to
			// generate a committed hash, and store it in MessageLeaves storage which can be
			// verified on Ethereum later.
			let abi_commands: Vec<CommandWrapper> = commands
				.into_iter()
				.map(|command| CommandWrapper {
					kind: command.kind,
					gas: command.gas,
					payload: Bytes::from(command.payload),
				})
				.collect();
			let committed_message = OutboundMessageWrapper {
				origin: FixedBytes::from(origin.as_fixed_bytes()),
				nonce,
				topic: FixedBytes::from(id.as_fixed_bytes()),
				commands: abi_commands,
			};
			let message_abi_encoded_hash =
				<T as Config>::Hashing::hash(&committed_message.abi_encode());
			MessageLeaves::<T>::append(message_abi_encoded_hash);

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

			Self::deposit_event(Event::MessageAccepted { id, nonce });

			Ok(true)
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

**File:** bridges/snowbridge/docs/v2.md (L99-102)
```markdown
The XCM bridge-router on AH will charge a small fee to prevent spamming BH with bridge messages. This is necessary since
the `ExportMessage` instruction in message $x_2$ will have no execution fee on BH. For a similar reason, we should also
impose a minimum relayer reward of at least the existential deposit 0.1 DOT, which acts as a deposit to stop spamming
messages with 0 rewards.
```
