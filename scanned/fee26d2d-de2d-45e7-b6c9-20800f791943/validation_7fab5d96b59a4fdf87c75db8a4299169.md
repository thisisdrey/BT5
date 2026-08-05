Based on my investigation, I found a genuine local analog to the reported bug class. The core broken invariant in the external report is: **a public composite entry point invokes two sub-operations in sequence where the safety mechanism designed to gate one operation is structurally bypassed/absent for the atomic combination**, leading to a public-underpriced-work / stalled-processing condition on bridge message flow.

### Title
Outbound Queue V2 has no operating-mode halt gate on message enqueue/delivery — governance cannot stop Snowbridge V2 outbound processing - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`pallet-snowbridge-outbound-queue` (v1) enforces a halt check in `SendMessage::deliver` before enqueuing a message: `ensure!(!Self::operating_mode().is_halted(), SendError::Halted)` [1](#0-0) . The V2 outbound queue pallet defines an `Error::Halted` variant and even an `Event::OperatingModeChanged` [2](#0-1) , implying it was intended to support the same halt mechanism as v1. However, there is no `OperatingMode` storage item, no `set_operating_mode` call, and the `deliver` implementation for V2 never checks any halted/operating-mode state before calling `T::MessageQueue::enqueue_message` [3](#0-2) .

### Finding Description
In v1, the composite send flow is `validate()` → `deliver()`, and `deliver()` is the exact point where the halt guard is enforced, mirroring the "check right before the state-changing action" pattern: [4](#0-3) 

In v2, the equivalent `deliver()` implementation performs no such guard at all: [3](#0-2) 

The V2 pallet's own `Error` enum still declares `Halted` [5](#0-4) , and `Event::OperatingModeChanged` exists [6](#0-5)  — both are vestiges of an intended halt mechanism (analogous to v1's `Self::operating_mode()`), but there is no storage backing it and no code path ever emits it or reads it in v2. `do_process_message`, which actually assigns a nonce, records the message for Ethereum-side commitment, and creates a `PendingOrder` for relayer reward, likewise contains no halt check: [7](#0-6) 

This is the same class of bug as the `depositAndBridge` report: a security gate (`shareUnlockTime` check / `operating_mode().is_halted()` check) that exists and functions correctly in one code path is absent when the equivalent action is invoked through a related/newer entry point, so the composite flow silently proceeds instead of being correctly blocked. In the report, the bug caused an over-restrictive failure (DoS); here it causes the opposite and more severe failure — a **missing** restriction, i.e., governance's halt/emergency-stop mechanism (`BasicOperatingMode`) that is load-bearing for v1 has no effect on v2's outbound gateway. Messages continue to be enqueued, processed, committed into the merkle root/header digest, and `PendingOrder`s (which carry relayer fee liabilities) continue to accumulate even while an operator/governance actor believes outbound delivery is halted.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" and "permanent... bridge-state lock" categories: if BridgeHub governance halts the bridge (e.g., in response to a compromised Ethereum-side gateway or emergency incident, as was done for the inbound verifier in the referenced fix [8](#0-7) ), the V2 outbound queue keeps accepting, processing, and committing messages to Ethereum and keeps creating fee-bearing `PendingOrder`s regardless. This defeats the emergency-stop invariant that governance relies on to prevent further state transitions during an incident, and continues to commit potentially compromised or unwanted outbound commands into the header digest that Ethereum's light client will accept as valid.

### Likelihood Explanation
Likelihood is Medium: this does not require a malicious actor — it triggers automatically any time governance halts (or attempts to halt) the V2 outbound queue via the mechanism used for v1, because v2 has no equivalent storage/gate to halt. Any legitimate sibling-chain message continues to flow through `deliver` → `do_process_message` unaffected.

### Recommendation
Add an `OperatingMode` storage item and `set_operating_mode` call to `pallet-snowbridge-outbound-queue-v2`, mirroring v1, and enforce the halt check in `SendMessage::deliver` (and/or `do_process_message`) before enqueueing/processing messages, exempting only the primary governance channel as v1 does.

### Proof of Concept
1. Deploy a runtime with `snowbridge-pallet-outbound-queue-v2` configured normally.
2. Observe that no extrinsic or storage item exists to halt this pallet (confirmed by absence of `OperatingMode` storage/`set_operating_mode` call in [9](#0-8) , contrasted with v1's `OperatingModeChanged`-backed halt in [4](#0-3) ).
3. Any sibling parachain/system-v2 call to `Pallet::validate`/`Pallet::deliver` succeeds and enqueues into `T::MessageQueue` unconditionally [3](#0-2) .
4. `do_process_message` runs to completion, inserting a `PendingOrder` and incrementing `Nonce`, and the merkle root is still committed at `on_finalize` via `commit()` [10](#0-9)  — with no way for governance to prevent this using the pallet's own primitives.

**Uncertainty note:** I was unable to fully verify within available iterations whether some other layer (e.g., `pallet-snowbridge-system-v2`, the `EnqueueMessage` implementation, or a shared cross-pallet halt flag) enforces a halt check upstream of `SendMessage::validate`/`deliver` for v2. My last search into `bridges/snowbridge/pallets/system-v2/src/lib.rs` (which does reference `Halted`/`operating_mode`) was cut off before I could confirm whether that check actually gates the V2 outbound send path or is unrelated (e.g., only gating inbound-v2 or governance calls). If `system-v2` does enforce an equivalent halt check before calling into outbound-queue-v2's `deliver`, this finding would be weakened or invalidated, and I recommend verifying this specific call chain (`system-v2::send` → `outbound-queue-v2::deliver`) before treating this as confirmed.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L76-88)
```rust
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = AggregateMessageOrigin::Snowbridge(ticket.channel_id);

		if ticket.channel_id != PRIMARY_GOVERNANCE_CHANNEL {
			ensure!(!Self::operating_mode().is_halted(), SendError::Halted);
		}

		let message = ticket.message.as_bounded_slice();

		T::MessageQueue::enqueue_message(message, origin);
		Self::deposit_event(Event::MessageQueued { id: ticket.message_id });
		Ok(ticket.message_id)
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L109-497)
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

	#[pallet::event]
	#[pallet::generate_deposit(pub fn deposit_event)]
	pub enum Event<T: Config> {
		/// Message has been queued and will be processed in the future
		MessageQueued {
			/// The message
			message: Message,
		},
		/// Message will be committed at the end of current block. From now on, to track the
		/// progress the message, use the `nonce` or the `id`.
		MessageAccepted {
			/// ID of the message
			id: H256,
			/// The nonce assigned to this message
			nonce: u64,
		},
		/// Message was not committed due to some failure condition, like an overweight message.
		MessageRejected {
			/// ID of the message, if known (e.g. if a message is corrupt, the ID will not be
			/// known).
			id: Option<H256>,
			/// The payload of the message. Useful for debugging purposes if the message
			/// cannot be decoded.
			payload: Vec<u8>,
			/// The error that was returned.
			error: ProcessMessageError,
		},
		/// Message was not committed due to being overweight or the current block is full.
		MessagePostponed {
			/// The payload of the message. Useful for debugging purposes if the message
			/// cannot be decoded.
			payload: Vec<u8>,
			/// The error that was returned.
			reason: ProcessMessageError,
		},
		/// Some messages have been committed
		MessagesCommitted {
			/// Merkle root of the committed messages
			root: H256,
			/// number of committed messages
			count: u64,
		},
		/// Set OperatingMode
		OperatingModeChanged { mode: BasicOperatingMode },
		/// Delivery Proof received
		MessageDelivered { nonce: u64 },
	}

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

	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T> {
		fn on_initialize(_: BlockNumberFor<T>) -> Weight {
			// Remove storage from previous block
			Messages::<T>::kill();
			MessageLeaves::<T>::kill();
			// Reserve some weight for the `on_finalize` handler
			T::WeightInfo::on_initialize() + T::WeightInfo::commit()
		}

		fn on_finalize(_: BlockNumberFor<T>) {
			Self::commit();
		}
	}

	#[cfg(feature = "runtime-benchmarks")]
	pub trait BenchmarkHelper<T> {
		fn initialize_storage(beacon_header: BeaconHeader, block_roots_root: H256);
	}

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

	impl<T: Config> Pallet<T> {
		/// Generate a messages commitment and insert it into the header digest
		pub(crate) fn commit() {
			let count = MessageLeaves::<T>::decode_len().unwrap_or_default() as u64;
			if count == 0 {
				return;
			}

			// Create merkle root of messages
			let root = merkle_root::<<T as Config>::Hashing, _>(MessageLeaves::<T>::stream_iter());

			let digest_item: DigestItem = SnowbridgeDigestItem::SnowbridgeV2(root).into();

			// Insert merkle root into the header digest
			<frame_system::Pallet<T>>::deposit_log(digest_item);

			T::OnNewCommitment::on_new_commitment(root);

			Self::deposit_event(Event::MessagesCommitted { root, count });
		}

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
	}

	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
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

**File:** prdoc/stable2603-2/pr_11856.prdoc (L1-25)
```text
title: 'Snowbridge: halt the Ethereum verifier when the bridge is in emergency stop'

doc:
  - audience: Runtime Dev
    description: |
      When `pallet-ethereum-client` is in `Halted` operating mode, its `Verifier::verify`
      implementation now short-circuits with the new `VerificationError::Halted` instead of
      attempting to verify Ethereum-side proofs.

      Previously, halting the light client only blocked new beacon header updates via
      `EthereumBeaconClient::submit`. Proof verification still ran, which meant
      `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt` could
      continue to process receipts and pay out relayer rewards from `PendingOrders` while
      governance had halted the bridge (e.g. after a suspected beacon light client compromise).

      Halting the verifier closes that gap in one place — covering both inbound dispatch and
      outbound delivery-receipt reward payments.

crates:
  - name: snowbridge-verification-primitives
    bump: major
  - name: snowbridge-pallet-outbound-queue-v2
    bump: major
  - name: snowbridge-pallet-ethereum-client
    bump: patch
```
