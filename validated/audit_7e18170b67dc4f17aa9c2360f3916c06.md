Audit Report

## Title
Zero-fee outbound message spam floods the BridgeHub outbound-queue-v2 / MessageQueue, DoS-ing Ethereum-bound relayer delivery - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs])

## Summary
`SendMessage::validate`/`deliver` in `bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs` enqueue any XCM-derived `Message` into `pallet-message-queue` without checking that `Message.fee` is non-zero, and `XcmConverter::extract_remote_fee` in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs` accepts an attacker-chosen `fee_amount` of zero. Because `do_process_message` enforces only a global `MaxMessagesPerBlock` cap shared across all origins, a stream of maximum-size zero-fee messages can occupy this shared cap every block and delay legitimate fee-paying messages.

## Finding Description
`SendMessage::validate` in `send_message_impl.rs` only enforces a payload-size bound, with no fee check: [1](#0-0) . `deliver` unconditionally calls `T::MessageQueue::enqueue_message` and emits `MessageQueued` regardless of fee: [2](#0-1) .

The `fee` field is derived by `XcmConverter::extract_remote_fee`, which only validates that the asset is Ether (`Here` location) and that the withdrawn/reserved amount covers the declared `PayFees` amount (`reserved_fee_amount >= fee_amount`) — it never enforces a non-zero or minimum-value floor: [3](#0-2) . Consequently `fee_amount = 0` is a fully valid input, and it flows straight into the constructed `Message`: [4](#0-3) .

Once enqueued, `do_process_message` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` checks only a *global* count against `MaxMessagesPerBlock` (via `MessageLeaves::decode_len()`), independent of origin or fee, and yields (`MessagePostponed`) once that global cap is reached: [5](#0-4) . Messages that pass this check are committed into `Messages`/`MessageLeaves` and recorded in `PendingOrders` with the attacker-controlled (possibly zero) `fee`, which is only cleared upon relayer delivery-receipt submission: [6](#0-5) .

On BridgeHub Westend, `MaxMessagesPerBlock` is configured as a fixed, shared constant (`ConstU32<32>`) for the whole runtime, applying uniformly to all origins including the low-friction `AssetHub` XCM path and the `snowbridge_pallet_system_v2` frontend: [7](#0-6) . There is no per-origin or fee-weighted admission control in this pipeline — any attacker able to route zero-fee `Message`s into this shared per-block cap can crowd out legitimate, fee-paying messages, which are yielded (`MessagePostponed`) and must wait for a subsequent block, repeatable indefinitely as long as the attacker keeps submitting.

## Impact Explanation
This matches the "public underpriced work that degrades block production or stalls bridge processing" impact category permitted by the gate. Zero-fee messages occupy the same fixed `MaxMessagesPerBlock` slot budget as legitimate messages, causing genuine users' messages to be repeatedly `MessagePostponed`, and growing `PendingOrders` with entries relayers have no economic incentive to service (since delivering a zero-fee order pays nothing), degrading BridgeHub state and stalling Ethereum-bound delivery.

## Likelihood Explanation
Exploitation only requires an ordinary, unprivileged user constructing an XCM `ExportMessage`/`PayFees` sequence with `amount = 0` (routed via AssetHub or the `snowbridge_pallet_system_v2` frontend), which is accepted without a minimum-fee check by `extract_remote_fee` and `SendMessage::validate`. No relayer collusion, governance action, or leaked key is needed, and the attack is trivially repeatable every block up to the shared cap. The design document for v2 (`bridges/snowbridge/docs/v2.md`) explicitly acknowledges the need for a minimum relayer reward "to stop spamming messages with 0 rewards," confirming the gap is a known but unenforced concern in the current pallet logic.

## Recommendation
- Enforce a minimum non-zero fee/reward threshold in `XcmConverter::extract_remote_fee` (`convert.rs`) and/or in `SendMessage::validate` (`send_message_impl.rs`), rejecting messages whose `fee` falls below a configured `MinimumReward`.
- Introduce per-origin rate limiting or fee-weighted admission ordering in the outbound-queue-v2 processing logic so that zero/low-fee messages cannot monopolize the shared `MaxMessagesPerBlock` budget.
- Add expiry/pruning for stale zero-fee `PendingOrders` entries to bound storage growth.

## Proof of Concept
1. On AssetHub, construct an XCM routed to the Snowbridge v2 exporter: `WithdrawAsset(ETH, 0)`, `PayFees{asset: ETH, amount: 0}`, `WithdrawAsset(<ENA>, 1)`, `AliasOrigin(attacker)`, `DepositAsset(...)`, `SetTopic(...)`.
2. `XcmConverter::extract_remote_fee` accepts `amount = 0` since no minimum is enforced, producing `Message{ fee: 0, .. }`.
3. `SendMessage::validate`/`deliver` in `send_message_impl.rs` accept and enqueue the message (only size-checked).
4. Repeat submission up to `MaxMessagesPerBlock` (32 on BridgeHub Westend) times per block; `do_process_message`'s global counter check causes legitimate concurrent messages to receive `MessagePostponed` events once the shared cap is filled by zero-fee traffic.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L95-117)
```rust
	fn extract_remote_fee(&mut self) -> Result<u128, XcmConverterError> {
		use XcmConverterError::*;
		let reserved_fee_assets = match_expression!(self.next()?, WithdrawAsset(fee), fee)
			.ok_or(WithdrawAssetExpected)?;
		ensure!(reserved_fee_assets.len() == 1, AssetResolutionFailed);
		let reserved_fee_asset =
			reserved_fee_assets.inner().first().cloned().ok_or(AssetResolutionFailed)?;
		let (reserved_fee_asset_id, reserved_fee_amount) = match reserved_fee_asset {
			Asset { id: asset_id, fun: Fungible(amount) } => Ok((asset_id, amount)),
			_ => Err(AssetResolutionFailed),
		}?;
		let fee_asset =
			match_expression!(self.next()?, PayFees { asset: fee }, fee).ok_or(InvalidFeeAsset)?;
		let (fee_asset_id, fee_amount) = match fee_asset {
			Asset { id: asset_id, fun: Fungible(amount) } => Ok((asset_id, *amount)),
			_ => Err(AssetResolutionFailed),
		}?;
		// Check the fee asset is Ether (XCM is evaluated in Ethereum context).
		ensure!(fee_asset_id.0 == Here.into(), InvalidFeeAsset);
		ensure!(reserved_fee_asset_id.0 == Here.into(), InvalidFeeAsset);
		ensure!(reserved_fee_amount >= fee_amount, InvalidFeeAsset);
		Ok(fee_amount)
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L312-317)
```rust
		let message = Message {
			id: (*topic_id).into(),
			origin,
			fee: fee_amount,
			commands: BoundedVec::try_from(commands).map_err(|_| TooManyCommands)?,
		};
```

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L422-440)
```rust
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
