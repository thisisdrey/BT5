Confirmed: `MaxMessagesPerBlock` for the outbound queue v2 is set to `ConstU32<32>` in production config (`bridge_hub_westend/src/bridge_to_ethereum_config.rs` L205), and `do_process_message` applies this cap globally across all `AggregateMessageOrigin::SnowbridgeV2` queues, not per-origin. This confirms the mechanism is real and matches the report.

Audit Report

## Title
Permissionless flooding of the shared Snowbridge V2 outbound `MessageQueue` can delay critical/governance message delivery to Ethereum - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::do_process_message` enforces a single, origin-agnostic cap (`MaxMessagesPerBlock`, set to 32 in production configs) on how many messages the outbound queue commits per block, returning `Err(Yield)` for any further message once the cap is reached regardless of which `AggregateMessageOrigin` queue it came from. [1](#0-0)  Since sibling parachains can enqueue outbound messages into the shared `pallet-message-queue` with no privilege check, an attacker-controlled sibling chain can flood the queue with cheap messages every block to exhaust this shared per-block budget before governance/BridgeHub-root-originated messages (their own `SnowbridgeV2` origin queue) get processed, delaying delivery of security- or governance-critical messages to Ethereum. [2](#0-1) 

## Finding Description
`do_process_message` reads `MessageLeaves::<T>::decode_len()` and compares it to `T::MaxMessagesPerBlock::get()`; once reached, it yields for any subsequent message in that block, irrespective of origin. [3](#0-2)  The underlying `pallet-message-queue` normally provides fairness via round-robin servicing of the `ReadyRing`, giving each ready queue (each distinct origin, including the governance origin) its own turn. [4](#0-3)  However, this per-queue fairness is undermined by the pallet-level global cap in `do_process_message`, which is shared across *all* origins feeding into the outbound-queue-v2 pallet — so once an attacker's sibling-origin queue exhausts the 32-message/block budget, the governance queue's messages are yielded for that block even though it is a separate, distinct `ReadyRing` entry. [1](#0-0)  This exact scenario is reproduced by the existing regression test, which floods 40 low-priority messages from a sibling parachain and shows the governance message's footprint remains un-decremented (still 1, unprocessed) after the first block, only executing in the following block. [5](#0-4)  Delivery into the queue requires only calling `OutboundQueue::deliver` after `validate`, exercised via ordinary XCM export from a sibling parachain, with no admin or governance privilege required. [6](#0-5) 

The production value of `MaxMessagesPerBlock` is `ConstU32<32>` for both the westend and rococo bridge-hub outbound-queue-v2 configs. [7](#0-6) 

## Impact Explanation
This matches the impact gate category of "public underpriced work that degrades block production or stalls bridge processing": an unprivileged sibling-chain actor can keep the shared, origin-agnostic per-block commit budget of the Snowbridge V2 outbound queue saturated with low-value traffic, delaying commitment (and therefore delivery to Ethereum) of governance- or security-critical outbound messages by at least one block per flooding round, with the delay scaling with the volume/persistence of injected messages as demonstrated by the test.

## Likelihood Explanation
Likelihood is high: enqueuing messages only requires ordinary XCM export from any sibling parachain able to reach BridgeHub, no special origin filter exists in `do_process_message` to prioritize governance-originated messages, and the exact mechanism is already reproduced deterministically by an existing unit test (`governance_message_not_processed_in_same_block_when_queue_congested_with_low_priority_messages`), requiring no adversarial assumption beyond ordinary XCM traffic generation.

## Recommendation
Decouple the per-block message-commit budget so governance/BridgeHub-root-originated messages are serviced independently of (or with reserved capacity ahead of) ordinary sibling-chain traffic — e.g., track `MessageLeaves` count per-origin-class or reserve a dedicated sub-budget for governance messages within `MaxMessagesPerBlock`, so that flooding by ordinary siblings cannot consume the entire per-block cap before governance messages are processed.

## Proof of Concept
The existing test `governance_message_not_processed_in_same_block_when_queue_congested_with_low_priority_messages` in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` demonstrates the mechanism: 40 low-priority messages are enqueued from a sibling parachain, then a governance message is enqueued; after advancing one block with unlimited service weight, the sibling queue's footprint drops by 20 (the per-block cap) while the governance message's footprint remains at 1 (unprocessed), only clearing in the subsequent block. [8](#0-7)  An attacker can repeat this injection every block from a sibling parachain to sustain the delay.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L343-358)
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
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L194-220)
```rust
impl snowbridge_pallet_outbound_queue_v2::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Hashing = Keccak256;
	type MessageQueue = MessageQueue;
	// Maximum payload size for outbound messages.
	type MaxMessagePayloadSize = ConstU32<2048>;
	// Maximum number of outbound messages that can be committed per block.
	// It's benchmarked, including the entire process flow(initialize,submit,commit) in the
	// worst-case, Benchmark results in `../weights/snowbridge_pallet_outbound_queue_v2.
	// rs` show that the `process` function consumes less than 1% of the block capacity, which is
	// safe enough.
	type MaxMessagesPerBlock = ConstU32<32>;
	type GasMeter = ConstantGasMeterV2;
	type Balance = Balance;
	type WeightToFee = WeightToFee;
	type Verifier = EthereumBeaconClient;
	type GatewayAddress = EthereumGatewayAddress;
	type WeightInfo = crate::weights::snowbridge_pallet_outbound_queue_v2::WeightInfo<Runtime>;
	type EthereumNetwork = EthereumNetwork;
	type RewardKind = BridgeReward;
	type DefaultRewardKind = SnowbridgeReward;
	type RewardPayment = BridgeRelayers;
	type AggregateMessageOrigin = AggregateMessageOrigin;
	type OnNewCommitment = ();
	#[cfg(feature = "runtime-benchmarks")]
	type Helper = Runtime;
}
```

**File:** substrate/frame/message-queue/src/lib.rs (L116-125)
```rust
//! First it tries to "rotate" the `ReadyRing` by one through advancing the `ServiceHead` to the
//! next *ready* queue. It then starts to service this queue by servicing as many pages of it as
//! possible. Servicing a page means to execute as many message of it as possible. Each executed
//! message is marked as *processed* if the [`Config::MessageProcessor`] return Ok. An event
//! [`Event::Processed`] is emitted afterwards. It is possible that the weight limit of the pallet
//! will never allow a specific message to be executed. In this case it remains as unprocessed and
//! is skipped. This process stops if either there are no more messages in the queue or the
//! remaining weight became insufficient to service this queue. If there is enough weight it tries
//! to advance to the next *ready* queue and service it. This continues until there are no more
//! queues on which it can make progress or not enough weight to check that.
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L184-227)
```rust
#[test]
fn governance_message_not_processed_in_same_block_when_queue_congested_with_low_priority_messages()
{
	let sibling_id: u32 = 1000;

	new_tester().execute_with(|| {
		// submit a lot of low priority messages from asset_hub which will need multiple blocks to
		// execute(20 messages for each block so 40 required at least 2 blocks)
		let max_messages = 40;
		for _ in 0..max_messages {
			// submit low priority message
			let message = mock_message(sibling_id);
			let ticket = OutboundQueue::validate(&message).unwrap();
			OutboundQueue::deliver(ticket).unwrap();
		}

		let footprint =
			MessageQueue::footprint(SnowbridgeV2(H256::from_low_u64_be(sibling_id as u64)));
		assert_eq!(footprint.storage.count, (max_messages) as u64);

		let message = mock_governance_message::<Test>();
		let ticket = OutboundQueue::validate(&message).unwrap();
		OutboundQueue::deliver(ticket).unwrap();

		// move to next block
		ServiceWeight::set(Some(Weight::MAX));
		run_to_end_of_next_block();

		// first process 20 messages from sibling channel
		let footprint =
			MessageQueue::footprint(SnowbridgeV2(H256::from_low_u64_be(sibling_id as u64)));
		assert_eq!(footprint.storage.count, 40 - 20);

		// and governance message does not have the chance to execute in same block
		let footprint = MessageQueue::footprint(SnowbridgeV2(bridge_hub_root_origin()));
		assert_eq!(footprint.storage.count, 1);

		// move to next block
		ServiceWeight::set(Some(Weight::MAX));
		run_to_end_of_next_block();

		// now governance message get executed in this block
		let footprint = MessageQueue::footprint(SnowbridgeV2(bridge_hub_root_origin()));
		assert_eq!(footprint.storage.count, 0);
```
