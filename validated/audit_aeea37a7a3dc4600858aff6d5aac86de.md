## Analog Identified

The Snowbridge Ethereum **outbound queue** enforces a hard, chain-wide per-block cap on how many messages may be committed for delivery to Ethereum, and this cap is **global across all channels/parachains**, not per-origin. This mirrors the external bug's core invariant break: a shared, hard-capped "capacity" resource that any unprivileged, fee-paying actor can exhaust, denying other users (including those trying to move funds/settle state) timely service, while only governance/root can adjust the limit or halt the queue.

### Title
Global per-block message cap in Snowbridge `OutboundQueue` lets a single channel monopolize commitment capacity and delay other users' cross-chain settlement - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`pallet_snowbridge_outbound_queue`'s `do_process_message` gates message commitment with a single, pallet-wide counter (`MessageLeaves`) compared against `T::MaxMessagesPerBlock` (32 in the Bridge Hub Westend runtime), regardless of which channel/parachain the message came from. [1](#0-0) [2](#0-1) 

### Finding Description
`MessageLeaves` is a single global `StorageValue`, shared by every registered channel (every sibling parachain plus the governance channel), and it is reset only once per block in `on_initialize`. [3](#0-2) 
Every call to `do_process_message` — irrespective of the message's originating channel — checks this same counter and yields once it hits `MaxMessagesPerBlock`, which is configured as `ConstU32<32>` in the runtime. [4](#0-3) 

Because the cap is shared rather than per-channel, whichever queue is serviced first within a block can consume the entire 32-message budget for that block, forcing every other channel's messages — including the high-priority governance channel — to `Yield` and wait for a subsequent block. The pallet's own test suite demonstrates this directly: 40 low-priority messages from a single sibling channel delay a governance message by a full block even though the module-level documentation states "processing of governance commands can never be halted." [5](#0-4) [6](#0-5) 

This is structurally analogous to the reported `RateLimiter` bug: a single shared, hard-capped resource (there: 1000 ETH/24h; here: 32 messages/block) that any fee-paying, unprivileged actor can exhaust through ordinary usage, starving other users (including those needing timely execution, e.g. urgent asset transfers/redemptions to Ethereum) of access to the same resource, with no per-origin fairness or reservation guarding against monopolization.

### Impact Explanation
Any parachain/channel with legitimate access to the Snowbridge exporter (e.g. AssetHub users doing routine token transfers) can, simply by submitting enough outbound messages in a block, consume the entire global 32-message commitment budget. This delays delivery of every other channel's messages to Ethereum, including governance operational messages and other users' asset transfers, undermining the documented liveness guarantee for governance and degrading bridge processing for all users — a direct instance of "public underpriced work that degrades block production or stalls bridge processing" against the impact gate.

### Likelihood Explanation
No governance, admin, relayer, validator, or malicious peer is required — an ordinary, fee-paying user (or a busy parachain under normal load) triggers this simply by generating outbound Snowbridge traffic. The behavior is already observed and codified in the pallet's own unit test, confirming it is a real code path rather than a theoretical scenario.

### Recommendation
Track `MessageLeaves`/commitment capacity per-channel (or reserve a minimum guaranteed slot per channel, especially for the governance channel) rather than as a single global counter, so that one channel's traffic volume cannot consume the entire per-block commitment budget and starve other channels.

### Proof of Concept
The existing test `governance_message_does_not_get_the_chance_to_processed_in_same_block_when_congest_of_low_priority_sibling_messages` already demonstrates the mechanism: it enqueues 40 messages from one sibling channel, then a governance message, and shows the governance message is not processed in the first serviced block because the shared capacity/queue servicing is consumed by the flooding channel first. [6](#0-5)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L32-36)
```rust
//! # Message Priorities
//!
//! The processing of governance commands can never be halted. This effectively
//! allows us to pause processing of normal user messages while still allowing
//! governance commands to be sent to Ethereum.
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L228-253)
```rust
	#[pallet::storage]
	#[pallet::unbounded]
	#[pallet::getter(fn message_leaves)]
	pub(super) type MessageLeaves<T: Config> = StorageValue<_, Vec<H256>, ValueQuery>;

	/// The current nonce for each message origin
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageMap<_, Twox64Concat, ChannelId, u64, ValueQuery>;

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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L307-313)
```rust
			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L179-192)
```rust
impl snowbridge_pallet_outbound_queue::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Hashing = Keccak256;
	type MessageQueue = MessageQueue;
	type Decimals = ConstU8<12>;
	type MaxMessagePayloadSize = ConstU32<2048>;
	type MaxMessagesPerBlock = ConstU32<32>;
	type GasMeter = ConstantGasMeter;
	type Balance = Balance;
	type WeightToFee = WeightToFee;
	type WeightInfo = crate::weights::snowbridge_pallet_outbound_queue::WeightInfo<Runtime>;
	type PricingParameters = EthereumSystem;
	type Channels = EthereumSystem;
}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L169-226)
```rust
#[test]
fn governance_message_does_not_get_the_chance_to_processed_in_same_block_when_congest_of_low_priority_sibling_messages(
) {
	use snowbridge_core::PRIMARY_GOVERNANCE_CHANNEL;
	use AggregateMessageOrigin::*;

	let sibling_id: u32 = 1000;
	let sibling_channel_id: ChannelId = ParaId::from(sibling_id).into();

	new_tester().execute_with(|| {
		// submit a lot of low priority messages from asset_hub which will need multiple blocks to
		// execute(20 messages for each block so 40 required at least 2 blocks)
		let max_messages = 40;
		for _ in 0..max_messages {
			// submit low priority message
			let message = mock_message(sibling_id);
			let (ticket, _) = OutboundQueue::validate(&message).unwrap();
			OutboundQueue::deliver(ticket).unwrap();
		}

		let footprint = MessageQueue::footprint(Snowbridge(sibling_channel_id));
		assert_eq!(footprint.storage.count, (max_messages) as u64);

		let message = mock_governance_message::<Test>();
		let (ticket, _) = OutboundQueue::validate(&message).unwrap();
		OutboundQueue::deliver(ticket).unwrap();

		// move to next block
		ServiceWeight::set(Some(Weight::MAX));
		run_to_end_of_next_block();

		// first process 20 messages from sibling channel
		let footprint = MessageQueue::footprint(Snowbridge(sibling_channel_id));
		assert_eq!(footprint.storage.count, 40 - 20);

		// and governance message does not have the chance to execute in same block
		let footprint = MessageQueue::footprint(Snowbridge(PRIMARY_GOVERNANCE_CHANNEL));
		assert_eq!(footprint.storage.count, 1);

		// move to next block
		ServiceWeight::set(Some(Weight::MAX));
		run_to_end_of_next_block();

		// now governance message get executed in this block
		let footprint = MessageQueue::footprint(Snowbridge(PRIMARY_GOVERNANCE_CHANNEL));
		assert_eq!(footprint.storage.count, 0);

		// and this time process 19 messages from sibling channel so we have 1 message left
		let footprint = MessageQueue::footprint(Snowbridge(sibling_channel_id));
		assert_eq!(footprint.storage.count, 1);

		// move to the next block, the last 1 message from sibling channel get executed
		ServiceWeight::set(Some(Weight::MAX));
		run_to_end_of_next_block();
		let footprint = MessageQueue::footprint(Snowbridge(sibling_channel_id));
		assert_eq!(footprint.storage.count, 0);
	});
}
```
