## Finding

### Title
`snowbridge-outbound-queue-v2` enqueues messages with an attacker-supplied, unvalidated `fee` field, allowing zero-cost/underpriced Ethereum-bound work — ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs])

### Summary
The LoopFi report's root cause is that a resource (quota) is provisioned before its economic parameter (rate) is bound, letting a caller consume the resource while it is priced at zero. The same structural gap exists in Snowbridge's V2 outbound pipeline: unlike the V1 `OutboundQueue`, which computes a mandatory delivery fee from governance-controlled `PricingParameters` before a message is admitted, the V2 `OutboundQueue::validate` never computes or checks any fee at all. The `fee` that ends up backing relayer compensation and is stored in `PendingOrders` is whatever the caller supplied in the `Message` struct — including `0`.

### Finding Description
In V1, `SendMessage::validate` always derives the fee from protocol state and returns it for the caller to actually pay: [1](#0-0) 

That fee is enforced by the caller (`snowbridge-pallet-system::send`) via an actual token transfer to the treasury: [2](#0-1) 

In V2, `SendMessage::validate` performs no fee computation whatsoever — it only checks payload size and hands back the caller-supplied `Message` unchanged: [3](#0-2) 

The `fee` field inside that `Message` is set directly by `snowbridge-pallet-system-v2::send`, using whatever `amount` was passed into the calling extrinsic — there is no minimum-fee check against `GasMeter`-computed real dispatch/processing cost: [4](#0-3) 

`register_token` and `add_tip` are reachable by `T::FrontendOrigin` (documented as accepting "all origins" for asset locations nested in the caller's own consensus system) and pass a caller-controlled `amount: u128` straight through as the message `fee`: [5](#0-4) 

Once enqueued, `do_process_message` in the outbound-queue-v2 pallet still performs the full real work regardless of the attached fee — nonce allocation, ABI encoding, Keccak256 hashing, GasMeter computation, and a `PendingOrders` storage write that is committed into the block's merkle root and eventually dispatched to Ethereum: [6](#0-5) 

The fee is only used later, at delivery-receipt time, to reward the relayer: [7](#0-6) 

This is the exact same bug class as the LoopFi report: a corruptible value (`order.fee`, analogous to the quota `rate`) that should be bound atomically to real cost at the point the resource is consumed, but instead defaults to whatever the caller supplies — including `0` — with no protocol-side floor enforced by `validate()`/`do_process_message`.

### Impact Explanation
Because `PendingOrders::<T>::fee` (the corrupted value) can be driven to `0` by any account able to reach `register_token`/`add_tip` through the system-frontend/system-v2 pallets, an attacker can force BridgeHub to perform the full local weight cost of message processing (decoding, hashing, merkle commitment, storage writes) and reserve Ethereum-side gas/dispatch slots for commands (e.g. `RegisterForeignToken`) without paying anything to compensate relayers. Because relayers are only paid via `order.fee` at `submit_delivery_receipt` time, zero-fee messages give no economic incentive for relayers to ever submit the corresponding delivery receipt, so the associated `PendingOrders` entry can remain unresolved indefinitely (bridge-state lock) while consuming per-block message-processing capacity that could otherwise service priced work — i.e. "public underpriced work that degrades block production or stalls bridge processing," a listed in-scope impact.

### Likelihood Explanation
`register_token` explicitly documents that "All origins are allowed" as long as the asset location is nested within the caller's own consensus, meaning the entry point is unprivileged and requires no governance, validator, relayer, or node compromise. Repeatedly calling it (or `add_tip` with amount `0`, which is explicitly rejected, but `register_token`'s `amount` has no such floor) is trivially repeatable, cheap in local transaction fees, and requires no race condition or malicious external actor — matching the Method's requirement of an unprivileged, public-entrypoint path.

### Recommendation
Mirror the V1 design: have `snowbridge-outbound-queue-v2::SendMessage::validate` compute a minimum required fee from `GasMeter`-derived costs (local weight + remote gas) and reject (or upgrade) any `Message` whose caller-supplied `fee` is below that floor, instead of trusting the caller-supplied value unconditionally. Enforce this atomically inside `validate`/`do_process_message`, not only as an economic incentive resolved later at `process_delivery_receipt`.

### Proof of Concept
1. As any account able to satisfy `T::FrontendOrigin` (e.g., a sibling parachain/XCM origin nested under its own consensus), call `snowbridge_pallet_system_v2::register_token` (via the frontend proxy) with `amount = 0` or a negligible value.
2. `Pallet::send` in `system-v2/src/lib.rs` builds `Message { fee: amount, .. }` and calls `OutboundQueue::validate`, which in `outbound-queue-v2/src/send_message_impl.rs` performs no fee check and returns `Ok(ticket)`.
3. `do_process_message` fully processes the message (nonce, ABI encoding, hashing, `PendingOrders` insert with `fee = 0`), consuming real BridgeHub block weight and reserving an Ethereum dispatch slot.
4. Because `order.fee == 0`, no relayer is economically incentivized to call `submit_delivery_receipt`; the `PendingOrders` entry for this nonce persists indefinitely, and the underlying `RegisterForeignToken` command is never executed on Ethereum, while the attacker can repeat the call cheaply to keep consuming block-processing capacity.

**Uncertainty noted:** I was not able to fully trace `snowbridge-pallet-system-frontend::swap_fee_asset_and_burn` (which converts a user-supplied `fee_asset` into `ether_gained` before it reaches `system-v2::register_token`) to confirm whether that swap path enforces any implicit minimum via AMM slippage/pool state. The core defect — `outbound-queue-v2::validate` never computing or enforcing a fee floor, unlike `outbound-queue` v1 — is directly confirmed from the code shown above regardless of that upstream swap behavior.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-61)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());

```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L412-430)
```rust
		fn send(channel_id: ChannelId, command: Command, pays_fee: PaysFee<T>) -> DispatchResult {
			let message = Message { id: None, channel_id, command };
			let (ticket, fee) =
				T::OutboundQueue::validate(&message).map_err(|err| Error::<T>::Send(err))?;

			let payment = match pays_fee {
				PaysFee::Yes(account) => Some((account, fee.total())),
				PaysFee::Partial(account) => Some((account, fee.local)),
				PaysFee::No => None,
			};

			if let Some((payer, fee)) = payment {
				T::Token::transfer(
					&payer,
					&T::TreasuryAccount::get(),
					fee,
					Preservation::Preserve,
				)?;
			}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L17-32)
```rust
impl<T> SendMessage for Pallet<T>
where
	T: Config,
{
	type Ticket = Message;

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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L209-249)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::register_token())]
		pub fn register_token(
			origin: OriginFor<T>,
			sender: Box<VersionedLocation>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let sender_location: Location =
				(*sender).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;

			let location = Self::reanchor(asset_location)?;
			let token_id = TokenIdOf::convert_location(&location)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			if !ForeignToNativeId::<T>::contains_key(token_id) {
				ForeignToNativeId::<T>::insert(token_id, location.clone());
			}

			let command = Command::RegisterForeignToken {
				token_id,
				name: metadata.name.into_inner(),
				symbol: metadata.symbol.into_inner(),
				decimals: metadata.decimals,
			};

			let message_origin = Self::location_to_message_origin(sender_location)?;
			Self::send(message_origin, command, amount)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.into(),
				foreign_token_id: token_id,
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L284-300)
```rust
	impl<T: Config> Pallet<T> {
		/// Send `command` to the Gateway from a specific origin/agent
		fn send(origin: H256, command: Command, fee: u128) -> DispatchResult {
			let message = Message {
				origin,
				id: frame_system::unique((origin, &command, fee)).into(),
				fee,
				commands: BoundedVec::try_from(vec![command]).unwrap(),
			};

			let ticket = <T as pallet::Config>::OutboundQueue::validate(&message)
				.map_err(|err| Error::<T>::Send(err))?;

			<T as pallet::Config>::OutboundQueue::deliver(ticket)
				.map_err(|err| Error::<T>::Send(err))?;
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L341-443)
```rust
		/// Process a message delivered by the MessageQueue pallet.
		/// IMPORTANT!! This method does not roll back storage changes on error.
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			let current_len = MessageLeaves::<T>::decode_len().unwrap_or(0);
			if current_len >= T::MaxMessagesPerBlock::get() as usize {
				Self::deposit_event(Event::MessagePostponed {
					payload: message.to_vec(),
					reason: Yield,
				});
				return Err(Yield);
			}

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-474)
```rust
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
