### Title
Unbounded `assets` array in Snowbridge Inbound Queue V2 `Message` allows underpriced execution in a fixed-weight extrinsic - (File: `bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs`)

### Summary
The external report's core defect is that untrusted, externally-sourced data is deserialized and persisted/acted on without any runtime schema or size validation, letting an attacker submit arbitrarily large/structured payloads that are processed as if they were bounded. The direct local analog is in the Snowbridge Ethereum→Polkadot delivery path: `Message` (decoded from an Ethereum `OutboundMessageAccepted` log) carries an `assets: Vec<EthereumAsset>` field with no maximum length enforced anywhere in decode, and the `submit` extrinsic that consumes it charges a fixed weight regardless of how many assets are present.

### Finding Description
`Message` is defined with a plain, unbounded `Vec<EthereumAsset>`: [1](#0-0) 

It is populated from the raw Ethereum event log by `extract_assets`, which loops over every asset entry present in the ABI-decoded `payload.assets` with no cap: [2](#0-1) 

The pallet's only public entrypoint, `submit`, verifies the header/receipt proof, decodes the `Message` via `TryFrom<&Log>`, and dispatches it — all under a single fixed weight `T::WeightInfo::submit()` that does not scale with the number of assets or the size of the decoded payload: [3](#0-2) 

Downstream, `MessageToXcm::convert` iterates the entire `assets` vector, splitting it into `reserve_deposit_assets` / `reserve_withdraw_assets` and packing them into single `ReserveAssetDeposited` / `WithdrawAsset` XCM instructions: [4](#0-3) 

Contrast this with the Outbound Queue V2 pallet, which explicitly bounds its `commands` collection via `try_into()` into a bounded type and rejects the message as `Corrupt` if the bound is exceeded: [5](#0-4) 

No equivalent bound/schema check exists for the inbound `assets` vector or for the raw XCM payload size beyond a decode-depth limit — there is no `MaxAssets`, `BoundedVec`, or length-based rejection anywhere in the inbound-queue-v2/message primitives. This is the same class of bug as the external report: the type system boundary (Solidity ABI decode → Rust `TryFrom`) is treated as sufficient trust, but no runtime size/structure limit is enforced before the data is used to drive execution and XCM construction, unlike the sibling outbound pallet which does enforce such a bound.

### Impact Explanation
Because `submit()`'s declared weight is fixed and independent of the number of `EthereumAsset` entries or the size of the ABI payload, a message containing a very large `assets` array (limited only by the Ethereum-side gas limit for emitting the log, which is far larger than what is economical to charge on the Substrate side) forces the pallet to perform unbounded iteration (`extract_assets`, and later the partition/`Vec::into()` construction of XCM `Assets`) while under-charging the actual execution cost. This falls into the "public underpriced work that degrades block production" impact category — an unprivileged party (any Ethereum account that can call `v2_sendMessage`, requiring no relayer collusion or governance access) can cause a relayer to submit a genuinely valid proof whose processing cost on Bridge Hub is disproportionate to the fixed weight charged, risking block time overruns for the pallet's `submit` extrinsic.

### Likelihood Explanation
No malicious relayer, validator, or governance actor is required — only an ordinary Ethereum-side caller of the Gateway contract crafting a message with many asset entries, and any honest relayer subsequently calling `submit` with the resulting valid proof. The proof/verification step (`T::Verifier::verify`) only attests to inclusion of the log, not to the size or shape of its `assets` array, so verification succeeding does not bound the exploit. This makes the path directly reachable through the pallet's only public dispatchable.

### Recommendation
Bound `Message.assets` (and the ABI-decoded `payload.assets`) to a fixed maximum length during decode in `TryFrom<&Log> for Message`/`extract_assets`, mirroring the `try_into()` bound already used in `outbound-queue-v2`. Reject with `MessageDecodeError`/`Error::InvalidMessage` if the limit is exceeded, and make `submit()`'s weight scale with the (now-bounded) asset count/payload size rather than using a single fixed constant.

### Proof of Concept
1. On Ethereum, call the Gateway's `v2_sendMessage` with a `Payload.assets` array containing the maximum number of `EthereumAsset` entries permitted by the Ethereum block gas limit (potentially hundreds to thousands of entries, since each entry is small ABI-encoded data).
2. A relayer observes the resulting `OutboundMessageAccepted` event, builds a valid receipt/execution proof, and calls `EthereumInboundQueueV2::submit(origin, event)`.
3. `T::Verifier::verify` succeeds (the proof is genuine), and `Message::try_from(&event.event_log)` decodes the full unbounded `assets` vector via `extract_assets` with no length check (`bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs:182-192`).
4. `process_message` → `MessageToXcm::convert` iterates all assets to build `reserve_deposit_assets`/`reserve_withdraw_assets` and constructs the XCM (`bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs:396-411`), all inside an extrinsic charged only `T::WeightInfo::submit()` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:184-198`), which does not scale with the asset count — demonstrating underpriced, unbounded on-chain work driven entirely by attacker-controlled Ethereum-side input.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L100-120)
```rust
#[derive(Clone, Encode, Decode, Debug, TypeInfo)]
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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L182-192)
```rust
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
}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L182-198)
```rust
		/// Submit an inbound message originating from the Gateway contract on Ethereum
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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L396-411)
```rust
		let mut reserve_deposit_assets = vec![];
		let mut reserve_withdraw_assets = vec![];

		for asset in message.assets {
			match asset {
				AssetTransfer::ReserveDeposit(asset) => reserve_deposit_assets.push(asset),
				AssetTransfer::ReserveWithdraw(asset) => reserve_withdraw_assets.push(asset),
			};
		}

		if !reserve_deposit_assets.is_empty() {
			instructions.push(ReserveAssetDeposited(reserve_deposit_assets.into()));
		}
		if !reserve_withdraw_assets.is_empty() {
			instructions.push(WithdrawAsset(reserve_withdraw_assets.into()));
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L390-402)
```rust
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
```
