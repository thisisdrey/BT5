### Title
Outbound Queue V2 Has No Operating-Mode Halt Mechanism — Admin Cannot Pause Message Processing or Relayer Reward Payouts - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
Every other Snowbridge bridge pallet (`outbound-queue` v1, `inbound-queue`, `inbound-queue-v2`, `parachains`, `grandpa`, `messages`) implements a `BasicOperatingMode`/`OperatingMode` storage item plus a root-gated `set_operating_mode` extrinsic that can flip the pallet into `Halted` and is checked before doing any state-changing work. `outbound-queue-v2` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`) has no such storage item, no `set_operating_mode` call, and no halt check anywhere in `do_process_message` or `submit_delivery_receipt`. This is the same class of bug as the reported "Admin Cannot Pause Vault Operations" issue: an operational pause control that exists everywhere else in the design is missing/unwired for this specific component, leaving admins unable to stop it in an emergency.

### Finding Description
The v1 outbound-queue pallet declares: [1](#0-0) 
which stores `OperatingMode` and exposes a root-only `set_operating_mode` call, and this mode is intended to gate message processing (per the pallet doc comment about halting non-governance messages).

`outbound-queue-v2`, which handles the newer message pipeline (enqueue → `do_process_message` → commit → `submit_delivery_receipt` → relayer reward payout), defines its `Config`, `Error`, storages (`Messages`, `MessageLeaves`, `Nonce`, `PendingOrders`) and `#[pallet::call]` block: [2](#0-1) [3](#0-2) 
but there is no `OperatingMode` storage value and the sole dispatchable is `submit_delivery_receipt` — there is no `set_operating_mode` call at all: [4](#0-3) 

`do_process_message`, which enqueues messages, assigns nonces, and creates `PendingOrder`s with attached fees, contains no halt check: [5](#0-4) 

`process_delivery_receipt`, which pays out relayer rewards via `T::RewardPayment::register_reward` and removes the `PendingOrder`, also contains no halt check: [6](#0-5) 

The `ProcessMessage` and `SendMessage` trait implementations that feed into these functions likewise have no operating-mode gate: [7](#0-6) [8](#0-7) 

For contrast, the `snowbridge-pallet-system-frontend` pallet correctly wires an `ExportOperatingMode` storage value into its `register_token` entrypoint via `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted)`: [9](#0-8) [10](#0-9) 
demonstrating the intended pattern that `outbound-queue-v2` fails to implement for its own message processing and reward-payout path.

### Impact Explanation
If an exploit, bad message format, misconfigured `GasMeter`/`WeightToFee`, or a compromised relayer submits malformed delivery receipts (subject only to `T::Verifier::verify`), governance/root has no way to halt `outbound-queue-v2` message processing or the `submit_delivery_receipt`-triggered reward payout path while investigating or preparing a runtime upgrade. Every other Snowbridge queue pallet can be halted instantly via `set_operating_mode`; this one cannot. This directly matches the "Polkadot SDK Pivots" concern that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here there is no emergency stop for that advancement at all, risking continued fund disbursement (`T::RewardPayment::register_reward`) or queue growth during an active incident.

### Likelihood Explanation
This is a structural/code-completeness gap, not a race condition — it is deterministically present in every block. Any operational incident affecting outbound message delivery or delivery-receipt processing on BridgeHub triggers this gap immediately, since there is no admin lever to pull. The bug requires no attacker action to manifest; it is a pure control-plane omission analogous to the reported `isPaused` never being settable.

### Recommendation
Add an `OperatingMode` (`BasicOperatingMode`) storage item and a root-gated `set_operating_mode` extrinsic to `outbound-queue-v2`, mirroring `outbound-queue` v1's implementation at `bridges/snowbridge/pallets/outbound-queue/src/lib.rs:237-278`. Gate `do_process_message` and `submit_delivery_receipt` (and ideally `SendMessage::validate`/`deliver`) with `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)`, consistent with the pattern already used in `snowbridge-pallet-system-frontend`.

### Proof of Concept
1. Deploy/observe a runtime with `outbound-queue-v2` configured as in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`.
2. Attempt to locate any root-callable extrinsic to halt the pallet — inspect the `#[pallet::call]` block; only `submit_delivery_receipt` (call_index 1) exists, with no `set_operating_mode` (compare to `bridges/snowbridge/pallets/outbound-queue/src/lib.rs:265-279` where call_index 0 is `set_operating_mode`).
3. Trigger continuous message enqueue/processing (`do_process_message`) and delivery-receipt submission/reward payout (`process_delivery_receipt`) — observe that no configuration or call exists to stop this pipeline short of a full runtime upgrade, unlike the v1 pallet or `system-frontend` pallet which can be halted in the same block via `set_operating_mode`.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L237-278)
```rust
	/// The current operating mode of the pallet.
	#[pallet::storage]
	#[pallet::getter(fn operating_mode)]
	pub type OperatingMode<T: Config> = StorageValue<_, BasicOperatingMode, ValueQuery>;

	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T>
	where
		T::AccountId: AsRef<[u8]>,
	{
		fn on_initialize(_: BlockNumberFor<T>) -> Weight {
			// Remove storage from previous block
			Messages::<T>::kill();
			MessageLeaves::<T>::kill();
			// Reserve some weight for the `on_finalize` handler
			T::WeightInfo::commit()
		}

		fn on_finalize(_: BlockNumberFor<T>) {
			Self::commit();
		}

		fn integrity_test() {
			let decimals = T::Decimals::get();
			assert!(decimals == 10 || decimals == 12, "Decimals should be 10 or 12");
		}
	}

	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Halt or resume all pallet operations. May only be called by root.
		#[pallet::call_index(0)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
		pub fn set_operating_mode(
			origin: OriginFor<T>,
			mode: BasicOperatingMode,
		) -> DispatchResult {
			ensure_root(origin)?;
			OperatingMode::<T>::put(mode);
			Self::deposit_event(Event::OperatingModeChanged { mode });
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L109-175)
```rust
#[frame_support::pallet]
pub mod pallet {
	use super::*;
	use frame_support::pallet_prelude::*;
	use frame_system::pallet_prelude::*;

	#[pallet::pallet]
	pub struct Pallet<T>(_);

	#[pallet::config]
	pub trait Config: frame_system::Config {
		#[allow(deprecated)]
		type RuntimeEvent: From<Event<Self>> + IsType<<Self as frame_system::Config>::RuntimeEvent>;

		type Hashing: Hash<Output = H256>;

		type AggregateMessageOrigin: FullCodec
			+ MaxEncodedLen
			+ Clone
			+ Eq
			+ PartialEq
			+ TypeInfo
			+ Debug
			+ From<H256>;

		type MessageQueue: EnqueueMessage<Self::AggregateMessageOrigin>;

		/// Measures the maximum gas used to execute a command on Ethereum
		type GasMeter: GasMeter;

		type Balance: Balance + From<u128>;

		/// Max bytes in a message payload
		#[pallet::constant]
		type MaxMessagePayloadSize: Get<u32>;

		/// Max number of messages processed per block
		#[pallet::constant]
		type MaxMessagesPerBlock: Get<u32>;

		/// Hook that is called whenever there is a new commitment.
		type OnNewCommitment: OnNewCommitment;

		/// Convert a weight value into a deductible fee based.
		type WeightToFee: WeightToFee<Balance = Self::Balance>;

		/// Weight information for extrinsics in this pallet
		type WeightInfo: WeightInfo;

		/// The verifier for delivery proof from Ethereum
		type Verifier: Verifier;

		/// Address of the Gateway contract
		#[pallet::constant]
		type GatewayAddress: Get<H160>;
		/// Reward discriminator type.
		type RewardKind: Parameter + MaxEncodedLen + Send + Sync + Copy + Clone;
		/// The default RewardKind discriminator for rewards allocated to relayers from this pallet.
		#[pallet::constant]
		type DefaultRewardKind: Get<Self::RewardKind>;
		/// Relayer reward payment.
		type RewardPayment: RewardLedger<Self::AccountId, Self::RewardKind, u128>;
		/// Ethereum NetworkId
		type EthereumNetwork: Get<NetworkId>;
		#[cfg(feature = "runtime-benchmarks")]
		type Helper: BenchmarkHelper<Self>;
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L225-271)
```rust
	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
		/// Invalid Channel
		InvalidChannel,
		/// Invalid Envelope
		InvalidEnvelope,
		/// Message verification error
		Verification(VerificationError),
		/// Invalid Gateway
		InvalidGateway,
		/// Pending nonce does not exist
		InvalidPendingNonce,
		/// Reward payment failed
		RewardPaymentFailed,
	}

	/// Messages to be committed in the current block. This storage value is killed in
	/// `on_initialize`, so will not end up bloating state.
	///
	/// Is never read in the runtime, only by offchain message relayers.
	/// Because of this, it will never go into the PoV of a block.
	///
	/// Inspired by the `frame_system::Pallet::Events` storage value
	#[pallet::storage]
	#[pallet::unbounded]
	pub type Messages<T: Config> = StorageValue<_, Vec<OutboundMessage>, ValueQuery>;

	/// Hashes of the ABI-encoded messages in the [`Messages`] storage value. Used to generate a
	/// merkle root during `on_finalize`. This storage value is killed in `on_initialize`, so state
	/// at each block contains only root hash of messages processed in that block. This also means
	/// it doesn't have to be included in PoV.
	#[pallet::storage]
	#[pallet::unbounded]
	pub type MessageLeaves<T: Config> = StorageValue<_, Vec<H256>, ValueQuery>;

	/// The current nonce for the messages
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageValue<_, u64, ValueQuery>;

	/// Pending orders to relay
	#[pallet::storage]
	pub type PendingOrders<T: Config> =
		StorageMap<_, Twox64Concat, u64, PendingOrder<BlockNumberFor<T>>, OptionQuery>;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-318)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T>
	where
		<T as frame_system::Config>::AccountId: From<[u8; 32]>,
	{
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
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L343-443)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/process_message_impl.rs (L11-28)
```rust
impl<T: Config> ProcessMessage for Pallet<T> {
	type Origin = T::AggregateMessageOrigin;
	fn process_message(
		message: &[u8],
		origin: Self::Origin,
		meter: &mut WeightMeter,
		_: &mut [u8; 32],
	) -> Result<bool, ProcessMessageError> {
		let weight = T::WeightInfo::do_process_message();
		if meter.try_consume(weight).is_err() {
			Self::deposit_event(Event::MessagePostponed {
				payload: message.to_vec(),
				reason: ProcessMessageError::Overweight(weight),
			});
			return Err(ProcessMessageError::Overweight(weight));
		}
		Self::do_process_message(origin, message)
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L17-44)
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

	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = ticket.origin.into();

		let message =
			BoundedVec::try_from(ticket.encode()).map_err(|_| SendError::MessageTooLarge)?;

		T::MessageQueue::enqueue_message(message.as_bounded_slice(), origin);
		Self::deposit_event(Event::MessageQueued { message: ticket.clone() });
		Ok(ticket.id)
	}
}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L190-208)
```rust
	/// The current operating mode for exporting to Ethereum.
	#[pallet::storage]
	#[pallet::getter(fn export_operating_mode)]
	pub type ExportOperatingMode<T: Config> = StorageValue<_, OperatingMode, ValueQuery>;

	#[pallet::call]
	impl<T: Config> Pallet<T>
	where
		<T as frame_system::Config>::AccountId: Into<Location>,
	{
		/// Set the operating mode for exporting messages to Ethereum.
		#[pallet::call_index(0)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
		pub fn set_operating_mode(origin: OriginFor<T>, mode: OperatingMode) -> DispatchResult {
			ensure_root(origin)?;
			ExportOperatingMode::<T>::put(mode);
			Self::deposit_event(Event::ExportOperatingModeChanged { mode });
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L230-231)
```rust
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
```
