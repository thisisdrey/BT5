Based on my research, I found a solid local analog in the Snowbridge outbound-queue-v2 fee-handling pipeline, which mirrors the external report's core flaw: a caller-supplied "fee" value that funds a later payout is never validated against any actual amount withdrawn/escrowed from the sender.

### Title
Snowbridge outbound-queue-v2 trusts an unverified `fee` field to fund relayer reward payouts - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs, bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
In `snowbridge-pallet-outbound-queue-v2`, the `SendMessage::validate` implementation only checks that the payload is not too large; it never verifies that the `fee` field carried in the `Message` struct corresponds to any value actually withdrawn or escrowed from the sender. [1](#0-0)  That same untrusted `fee` is later persisted verbatim into `PendingOrder` when the message is processed, and is paid out to the relayer as a reward once a delivery receipt is submitted. [2](#0-1)  This is structurally the same defect as `Starklane::depositTokens`: a value that is supposed to represent "money already paid for this cross-chain action" is taken at face value with no on-chain check that it was actually collected.

### Finding Description
The v2 send pipeline is documented as:
1. `SendMessage::validate` — validate the message
2. `SendMessage::deliver` — enqueue for processing
3. `do_process_message` — decode, build `PendingOrder{nonce, fee, block_number}`
4. Later, `submit_delivery_receipt` pays the reward tied to that order's `fee` to the relayer. [3](#0-2) 

Compare this to the v1 pipeline: `SendMessage::validate` in v1 *computes* the fee itself from `GasMeter` and `PricingParameters`, returning `(ticket, fee)` so the caller can actually charge that exact amount before delivery. [4](#0-3) 

In v2, this guarantee is gone: `validate()` takes the `Message` (which already embeds an arbitrary `fee: u128` chosen by whoever constructed the `Message`) and returns only a `Ticket`—no computed/independent fee is returned or compared. [5](#0-4)  `deliver()` simply enqueues the ticket and emits an event—no balance is touched. [6](#0-5) 

Downstream, `do_process_message` decodes `Message{origin, id, fee, commands}` directly from the queued bytes and writes the `fee` field straight into `PendingOrder`—no cross-check against any escrow, reserved balance, or previously-charged amount. [7](#0-6) 

The only caller I could confirm computes `fee` from an actually-burned asset is `snowbridge_pallet_system_v2::Pallet::send`, invoked by `register_token`/`add_tip`, where the `amount` passed is the real `ether_gained` from `swap_fee_asset_and_burn` (a genuine burn of user funds). [8](#0-7) [9](#0-8)  For `upgrade` and `set_operating_mode`, `fee` is hardcoded to `0`. [10](#0-9) 

However, the module doc explicitly states messages also arrive via `EthereumBlobExporter::deliver` (XCM export path), which is a second, independent producer of `v2::Message` objects. [11](#0-10)  I was not able to fully trace, within the available tool budget, whether that exporter path enforces an equivalent burn/withdraw of exactly `fee` before constructing the `Message`, or whether it (or any other/future `SendMessage` caller) could construct a `Message` with an inflated `fee` that the pallet's own `validate`/`deliver`/`do_process_message` code would accept unquestioned. Since `SendMessage::validate` for v2 performs no fee computation or verification itself (unlike v1), the guarantee that `fee == actually collected amount` rests entirely on every external caller behaving correctly — there is no defense-in-depth check inside the outbound-queue-v2 pallet itself.

### Impact Explanation
`PendingOrder.fee` is the amount paid to the relayer via `RewardPayment: RewardLedger` when `submit_delivery_receipt` succeeds. [12](#0-11)  If any caller of `SendMessage::validate`/`deliver` can supply a `fee` value that was not actually collected/escrowed from a real account (i.e., without the pallet itself verifying it), the reward paid out at settlement time is unbacked — funds are minted/paid to relayers without ever having been taken from the message sender. That directly matches the "theft or unbacked mint" / "duplicate settlement or payout" impact categories for this program.

### Likelihood Explanation
Medium-to-uncertain: confirmed safe for the `system-v2::send` path (fee is tied to a real burn), but the outbound-queue-v2 pallet's own `validate`/`deliver`/`do_process_message` code path provides no independent enforcement, relying entirely on every current and future `SendMessage` caller (including the XCM `EthereumBlobExporter` v2 path, which I could not fully verify within this session) to correctly compute and collect `fee` before submission. Any caller that fails to do so — now or after a future code change — would immediately produce unbacked relayer rewards with no additional guard rail catching the error.

### Recommendation
Move fee computation/verification inside `snowbridge-pallet-outbound-queue-v2`'s `SendMessage::validate` (mirroring the v1 design, which returns a computed `Fee` for the caller to charge and can be checked independently), or add an explicit invariant check in `do_process_message`/`PendingOrder` construction that the message's `fee` field matches an amount actually reserved/escrowed for that specific `message.id`/`origin`, rejecting messages where this cannot be proven.

### Proof of Concept
Concrete PoC could not be finalized because I was unable to inspect the `EthereumBlobExporter` v2 `deliver` implementation (its fee-charging logic) within the remaining tool budget — this is the piece needed to confirm whether an unprivileged XCM caller can actually reach `do_process_message` with an inflated, uncollected `fee`. This should be verified with a Devin session that has full repository access before treating this as conclusively exploitable; the code-level gap (no fee verification in `outbound-queue-v2::send_message_impl.rs`/`do_process_message`) is confirmed, but end-to-end exploitability via the XCM exporter path is unconfirmed.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L34-43)
```rust
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = ticket.origin.into();

		let message =
			BoundedVec::try_from(ticket.encode()).map_err(|_| SendError::MessageTooLarge)?;

		T::MessageQueue::enqueue_message(message.as_bounded_slice(), origin);
		Self::deposit_event(Event::MessageQueued { message: ticket.clone() });
		Ok(ticket.id)
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L6-41)
```rust
//!
//! Messages come either from sibling parachains via XCM, or BridgeHub itself
//! via the `snowbridge-pallet-system-v2`:
//!
//! 1. `snowbridge_outbound_queue_primitives::v2::EthereumBlobExporter::deliver`
//! 2. `snowbridge_pallet_system_v2::Pallet::send`
//!
//! The message submission pipeline works like this:
//! 1. The message is first validated via the implementation for
//!    [`snowbridge_outbound_queue_primitives::v2::SendMessage::validate`]
//! 2. The message is then enqueued for later processing via the implementation for
//!    [`snowbridge_outbound_queue_primitives::v2::SendMessage::deliver`]
//! 3. The underlying message queue is implemented by [`Config::MessageQueue`]
//! 4. The message queue delivers messages to this pallet via the implementation for
//!    [`frame_support::traits::ProcessMessage::process_message`]
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L164-170)
```rust
		/// Reward discriminator type.
		type RewardKind: Parameter + MaxEncodedLen + Send + Sync + Copy + Clone;
		/// The default RewardKind discriminator for rewards allocated to relayers from this pallet.
		#[pallet::constant]
		type DefaultRewardKind: Get<Self::RewardKind>;
		/// Relayer reward payment.
		type RewardPayment: RewardLedger<Self::AccountId, Self::RewardKind, u128>;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L361-436)
```rust
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-74)
```rust
	fn validate(
		message: &Message,
	) -> Result<(Self::Ticket, Fee<<Self as SendMessageFeeProvider>::Balance>), SendError> {
		// The inner payload should not be too large
		let payload = message.command.abi_encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		// Ensure there is a registered channel we can transmit this message on
		ensure!(T::Channels::contains(&message.channel_id), SendError::InvalidChannel);

		// Generate a unique message id unless one is provided
		let message_id: H256 = message
			.id
			.unwrap_or_else(|| unique((message.channel_id, &message.command)).into());

		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());

		let queued_message: VersionedQueuedMessage = QueuedMessage {
			id: message_id,
			channel_id: message.channel_id,
			command: message.command.clone(),
		}
		.into();
		// The whole message should not be too large
		let encoded = queued_message.encode().try_into().map_err(|_| SendError::MessageTooLarge)?;

		let ticket = Ticket { message_id, channel_id: message.channel_id, message: encoded };

		Ok((ticket, fee))
	}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L155-200)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight((<T as pallet::Config>::WeightInfo::upgrade(), DispatchClass::Operational))]
		pub fn upgrade(
			origin: OriginFor<T>,
			impl_address: H160,
			impl_code_hash: H256,
			initializer: Initializer,
		) -> DispatchResult {
			let origin_location = T::GovernanceOrigin::ensure_origin(origin)?;
			let origin = Self::location_to_message_origin(origin_location)?;

			ensure!(
				!impl_address.eq(&H160::zero()) && !impl_code_hash.eq(&H256::zero()),
				Error::<T>::InvalidUpgradeParameters
			);

			let initializer_params_hash: H256 = blake2_256(initializer.params.as_ref()).into();

			let command = Command::Upgrade { impl_address, impl_code_hash, initializer };
			Self::send(origin, command, 0)?;

			Self::deposit_event(Event::<T>::Upgrade {
				impl_address,
				impl_code_hash,
				initializer_params_hash,
			});
			Ok(())
		}

		/// Sends a message to the Gateway contract to change its operating mode
		///
		/// Fee required: No
		///
		/// - `origin`: Must be `GovernanceOrigin`
		#[pallet::call_index(1)]
		#[pallet::weight((<T as pallet::Config>::WeightInfo::set_operating_mode(), DispatchClass::Operational))]
		pub fn set_operating_mode(origin: OriginFor<T>, mode: OperatingMode) -> DispatchResult {
			let origin_location = T::GovernanceOrigin::ensure_origin(origin)?;
			let origin = Self::location_to_message_origin(origin_location)?;

			let command = Command::SetOperatingMode { mode };
			Self::send(origin, command, 0)?;

			Self::deposit_event(Event::<T>::SetOperatingMode { mode });
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L372-404)
```rust
		fn swap_fee_asset_and_burn(
			origin: Location,
			fee_asset: Asset,
		) -> Result<u128, DispatchError> {
			let ether_location = T::EthereumLocation::get();
			let (fee_asset_location, fee_amount) = match fee_asset {
				Asset { id: AssetId(ref loc), fun: Fungible(amount) } => (loc, amount),
				_ => {
					tracing::debug!(target: LOG_TARGET, ?fee_asset, "error matching fee asset");
					return Err(Error::<T>::UnsupportedAsset.into());
				},
			};
			if fee_amount == 0 {
				return Ok(0);
			}

			let ether_gained = if *fee_asset_location != ether_location {
				Self::swap_and_burn(
					origin.clone(),
					fee_asset_location.clone(),
					ether_location,
					fee_amount,
				)
				.inspect_err(|&e| {
					tracing::debug!(target: LOG_TARGET, ?e, "error swapping asset");
				})?
			} else {
				burn_for_teleport::<T::AssetTransactor>(&origin, &fee_asset)
					.map_err(|_| Error::<T>::BurnError)?;
				fee_amount
			};
			Ok(ether_gained)
		}
```
