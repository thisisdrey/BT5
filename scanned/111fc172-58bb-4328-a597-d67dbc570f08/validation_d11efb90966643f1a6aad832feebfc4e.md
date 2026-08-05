Confirmed: `message.assets: Vec<EthereumAsset>` [1](#0-0)  is decoded from an arbitrary-length Solidity array with no cap in `extract_assets` [2](#0-1) , and the `submit` extrinsic charges only the fixed `T::WeightInfo::submit()` weight regardless of `assets.len()` [3](#0-2) .

### Title
Unbounded `assets` array in Snowbridge V2 inbound messages allows underpriced XCM construction that can overweight/stall AssetHub message processing and permanently strand bridged funds - (File: `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`)

### Summary
`snowbridge-pallet-inbound-queue-v2::submit` verifies an Ethereum log, decodes it into a `Message` whose `assets: Vec<EthereumAsset>` field has no length bound, then converts every element into a `ReserveDeposit`/`ReserveWithdraw` XCM instruction via `MessageToXcm::prepare` [4](#0-3)  and forwards the resulting XCM to AssetHub over XCMP [5](#0-4) . The extrinsic weight is fixed and independent of the asset count [6](#0-5) .

### Finding Description
Because the Ethereum `Gateway` contract can emit a `Payload` with an arbitrarily long `assets` array (bounded only by Ethereum-side gas, which is far cheaper per byte than Substrate PoV/weight economics), a relayer can submit a `submit()` call whose declared weight (`T::WeightInfo::submit()`) does not scale with `message.assets.len()`. Internally, `extract_assets` loops over every element with no cap and no error path for "too many assets" [2](#0-1) , and `MessageToXcm::prepare` pushes one `AssetTransfer` instruction per asset into the outgoing `Xcm<()>` [4](#0-3) . This XCM is queued for AssetHub via XCMP, then executed by the AssetHub message-queue/xcm-executor pipeline, where the instructions-per-message limits (`WeightInfoBounds`, `instructions_left`) and per-block weight budget of `pallet-message-queue`/`cumulus-pallet-xcmp-queue` apply. A message with enough embedded assets can exceed the AssetHub XCM weight limit or hit `ExceedsStackLimit`/`WeightLimitReached` in `weight_with_limit` [7](#0-6) , or exceed the `overweight_limit`/single-message weight ceiling in `pallet-message-queue`'s `service_queue`/`do_execute_overweight_inner` flow [8](#0-7) , causing the message to become permanently unprocessable (or perpetually re-queued as overweight, never executed within available weight since it's larger than any single-block/single-message allowance). Critically, on the Ethereum side the assets were already locked/burned as part of the `OutboundMessageAccepted` event that triggered this inbound message (an irreversible action equivalent to the analog report's "lock on the source chain"). If the corresponding XCM can never successfully execute the `ReserveWithdraw`/`ReserveDeposit` and `DepositAsset` on AssetHub, the tokens are permanently stranded — mirroring the referenced `releaseOnEid` bug where excessive per-call work causes gas exhaustion on the receiving chain after the source-chain state change is already final.

### Impact Explanation
This is a High-impact, public, unprivileged pathway: any account can call `submit()` with a proof for a legitimately-emitted Ethereum event (no malicious relayer/validator/governance assumption needed — the attacker only needs to trigger the Ethereum Gateway to emit a payload with a large `assets` array, which is an ordinary user action on the Ethereum side). It can permanently lock user funds/bridge state on the Substrate side, and because it stresses the message-queue/XCMP weight-accounting layer, it can also degrade or stall bridge processing for AssetHub if this queue starves ready processing slots for other messages in the same book/origin.

### Likelihood Explanation
Likelihood is Low-to-Medium: the fixed `submit()` weight and the unbounded asset vector are both directly evidenced in-repo, but successfully triggering irreversible unprocessability further depends on the exact configured weight limits (`overweight_limit`, `IdleMaxServiceWeight`, per-page/per-instruction bounds) in the specific AssetHub/BridgeHub runtime configuration — parameters not fully visible for confirmation. It requires no privileged actor, only ordinary interaction with the Ethereum Gateway contract to produce a message with many assets and a submit against BridgeHub.

### Recommendation
- Enforce a hard `MaxAssetsPerMessage` bound when decoding `Message.assets` in `extract_assets` / `TryFrom<&Log> for Message`, rejecting proofs whose asset count exceeds the bound before conversion.
- Make `submit()`'s declared weight (`WeightInfo::submit`) scale with the decoded asset count (similar to how `receive_messages_proof_weight` scales with `messages_count` in the messages pallet [9](#0-8) ), so PoV/weight is charged proportionally and cannot be underpriced.
- Ensure the generated XCM's instruction count/weight is validated against the destination chain's known execution limits before sending, failing fast (and reverting the whole extrinsic, since nonce marking happens transactionally) rather than allowing an unexecutable, overweight message to be queued.

### Proof of Concept
1. On Ethereum, call the Gateway contract's send-message function such that `OutboundMessageAccepted`'s `Payload.assets` array contains a very large number of `EthereumAsset` entries (e.g., thousands of `NativeTokenERC20`/`ForeignTokenERC20` entries), each with nonzero `value`. This is gas-cheap on Ethereum relative to the resulting Substrate-side cost.
2. A relayer (or the attacker) calls `snowbridge_pallet_inbound_queue_v2::submit(event)` on BridgeHub with a valid proof for that log. `T::Verifier::verify` succeeds, `Message::try_from` decodes all assets via `extract_assets` with no cap [10](#0-9) , and the extrinsic is charged only the fixed `submit()` weight.
3. `Self::process_message` marks the nonce processed and invokes `MessageToXcm::prepare`, which emits one `AssetTransfer` instruction per array element, producing an XCM with thousands of instructions [4](#0-3) .
4. This oversized XCM is sent via `Sender::deliver` to AssetHub's XCMP inbound queue [11](#0-10) .
5. On AssetHub, `pallet-message-queue`/`WeightInfoBounds::weight_with_limit` cannot execute the message within any configured single-message weight ceiling, marking it permanently overweight/unprocessable [12](#0-11) ; the assets that were already locked/burned on Ethereum are never reserve-withdrawn/deposited to the beneficiary, resulting in permanently stuck funds.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L101-120)
```rust
pub struct Message {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// A nonce for enforcing replay protection and ordering.
	pub nonce: u64,
	/// The address on Ethereum that initiated the message.
	pub origin: H160,
	/// The assets sent from Ethereum (ERC-20s).
	pub assets: Vec<EthereumAsset>,
	/// The command originating from the Gateway contract.
	pub payload: Payload,
	/// The claimer in the case that funds get trapped. Expected to be an XCM::v5::Location.
	pub claimer: Option<Vec<u8>>,
	/// Native ether bridged over from Ethereum
	pub value: u128,
	/// Fee in eth to cover the xcm execution on AH.
	pub execution_fee: u128,
	/// Relayer reward in eth. Needs to cover all costs of sending a message.
	pub relayer_fee: u128,
}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L144-191)
```rust
impl TryFrom<&Log> for Message {
	type Error = MessageDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		// Convert to B256 for Alloy decoding
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		// Decode the Solidity event from raw logs
		let event = IGatewayV2::OutboundMessageAccepted::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| MessageDecodeError)?;

		let event_payload = event.payload;

		let substrate_assets = Self::extract_assets(&event_payload)?;

		let message_payload = Payload::try_from(&event_payload)?;

		let mut claimer = None;
		if event_payload.claimer.len() > 0 {
			claimer = Some(event_payload.claimer.to_vec());
		}

		let message = Message {
			gateway: log.address,
			nonce: event.nonce,
			origin: H160::from(event_payload.origin.as_ref()),
			assets: substrate_assets,
			payload: message_payload,
			claimer,
			value: event_payload.value,
			execution_fee: event_payload.executionFee,
			relayer_fee: event_payload.relayerFee,
		};

		Ok(message)
	}
}

impl Message {
	fn extract_assets(
		payload: &IGatewayV2::Payload,
	) -> Result<Vec<EthereumAsset>, MessageDecodeError> {
		let mut substrate_assets = vec![];
		for asset in &payload.assets {
			substrate_assets.push(EthereumAsset::try_from(asset)?);
		}
		Ok(substrate_assets)
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L183-198)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::submit())]
		pub fn submit(origin: OriginFor<T>, event: Box<EventProof>) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);

			// submit message for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into a bridge message
			let message =
				Message::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidMessage)?;

			Self::process_message(who, message)
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L159-200)
```rust
		let mut assets = vec![];

		if message.value > 0 {
			// Asset for remaining ether
			let remaining_ether_asset: Asset = (ether_location.clone(), message.value).into();
			assets.push(AssetTransfer::ReserveDeposit(remaining_ether_asset));
		}

		for asset in &message.assets {
			match asset {
				EthereumAsset::NativeTokenERC20 { token_id, value } => {
					ensure!(*token_id != H160::zero(), ConvertMessageError::InvalidAsset);
					let token_location: Location = Location::new(
						2,
						[
							GlobalConsensus(EthereumNetwork::get()),
							AccountKey20 { network: None, key: (*token_id).into() },
						],
					);
					let asset: Asset = (token_location, *value).into();
					assets.push(AssetTransfer::ReserveDeposit(asset));
				},
				EthereumAsset::ForeignTokenERC20 { token_id, value } => {
					let asset_location = ConvertAssetId::maybe_convert(*token_id)
						.ok_or(ConvertMessageError::InvalidAsset)?;
					let asset_hub_from_ethereum: Location = Location::new(
						1,
						[
							GlobalConsensus(LocalNetwork::get()),
							Parachain(AssetHubParaId::get().into()),
						],
					);
					let ethereum_universal: InteriorLocation =
						[GlobalConsensus(EthereumNetwork::get())].into();
					let reanchored_asset_location = asset_location
						.reanchored(&asset_hub_from_ethereum, &ethereum_universal)
						.map_err(|_| ConvertMessageError::CannotReanchor)?;
					let asset: Asset = (reanchored_asset_location, *value).into();
					assets.push(AssetTransfer::ReserveWithdraw(asset));
				},
			}
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs (L54-73)
```rust
	pub fn process_xcm(
		who: T::AccountId,
		message: Message,
	) -> Result<XcmHash, MessageProcessorError> {
		// Convert the message to XCM
		let xcm = Converter::convert(message).map_err(|error| {
			tracing::error!(target: LOG_TARGET, ?error, "XCM conversion failed with error");
			MessageProcessorError::ConvertMessage(error)
		})?;

		// Forward XCM to a target location
		let dest = TargetLocation::get();
		let message_id = Self::send_xcm(dest.clone(), &who, xcm.clone()).map_err(|error| {
			tracing::error!(target: LOG_TARGET, ?error, ?dest, ?xcm, "XCM send failed with error");
			MessageProcessorError::SendMessage(error)
		})?;

		// Return the message_id
		Ok(message_id)
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs (L86-109)
```rust
	fn send_xcm(
		dest: Location,
		fee_payer: &T::AccountId,
		xcm: Xcm<()>,
	) -> Result<XcmHash, SendError> {
		let fee_payer = AccountToLocation::try_convert(fee_payer).map_err(|err| {
			tracing::error!(
				target: LOG_TARGET,
				?err,
				"Failed to convert account to XCM location",
			);
			SendError::NotApplicable
		})?;
		let (ticket, fee) = validate_send::<Sender>(dest, xcm)?;
		Executor::charge_fees(fee_payer, fee).map_err(|error| {
			tracing::error!(
				target: LOG_TARGET,
				?error,
				"Charging fees failed with error",
			);
			SendError::Fees
		})?;
		Sender::deliver(ticket)
	}
```

**File:** polkadot/xcm/xcm-builder/src/weight.rs (L173-198)
```rust
	fn weight_with_limit(
		message: &mut Xcm<C>,
		instructions_left: &mut u32,
		weight_limit: Weight,
	) -> Result<Weight, InstructionError> {
		let mut total_weight: Weight = Weight::zero();
		for (index, instruction) in message.0.iter_mut().enumerate() {
			let index = index.try_into().unwrap_or(u8::MAX);
			*instructions_left = instructions_left
				.checked_sub(1)
				.ok_or_else(|| InstructionError { index, error: XcmError::ExceedsStackLimit })?;
			let instruction_weight =
				&Self::instr_weight_with_limit(instruction, instructions_left, weight_limit)
					.map_err(|error| InstructionError { index, error })?;
			total_weight = total_weight
				.checked_add(instruction_weight)
				.ok_or(InstructionError { index, error: XcmError::Overflow })?;
			if total_weight.any_gt(weight_limit) {
				return Err(InstructionError {
					index,
					error: XcmError::WeightLimitReached(total_weight),
				});
			}
		}
		Ok(total_weight)
	}
```

**File:** substrate/frame/message-queue/src/lib.rs (L1081-1116)
```rust
	fn do_execute_overweight_inner(
		origin: MessageOriginOf<T>,
		page_index: PageIndex,
		index: T::Size,
		weight_limit: Weight,
	) -> Result<Weight, Error<T>> {
		let mut book_state = BookStateFor::<T>::get(&origin);
		ensure!(!T::QueuePausedQuery::is_paused(&origin), Error::<T>::QueuePaused);

		let mut page = Pages::<T>::get(&origin, page_index).ok_or(Error::<T>::NoPage)?;
		let (pos, is_processed, payload) =
			page.peek_index(index.into() as usize).ok_or(Error::<T>::NoMessage)?;
		let payload_len = payload.len() as u64;
		ensure!(
			page_index < book_state.begin ||
				(page_index == book_state.begin && pos < page.first.into() as usize),
			Error::<T>::Queued
		);
		ensure!(!is_processed, Error::<T>::AlreadyProcessed);
		use MessageExecutionStatus::*;
		let mut weight_counter = WeightMeter::with_limit(weight_limit);
		match Self::process_message_payload(
			origin.clone(),
			page_index,
			index,
			payload,
			&mut weight_counter,
			Weight::MAX,
			// ^^^ We never recognise it as permanently overweight, since that would result in an
			// additional overweight event being deposited.
		) {
			Overweight | InsufficientWeight => Err(Error::<T>::InsufficientWeight),
			StackLimitReached | Unprocessable { permanent: false } => {
				Err(Error::<T>::TemporarilyUnprocessable)
			},
			Unprocessable { permanent: true } | Processed => {
```

**File:** bridges/modules/messages/src/lib.rs (L212-213)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::receive_messages_proof_weight(&**proof, *messages_count, *dispatch_weight))]
```
