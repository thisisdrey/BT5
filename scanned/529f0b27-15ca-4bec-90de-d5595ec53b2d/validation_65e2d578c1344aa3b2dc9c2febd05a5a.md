Confirmed: the `submit` extrinsic in `snowbridge-pallet-inbound-queue-v2` is charged a **flat, fixed weight** — `#[pallet::weight(T::WeightInfo::submit())]` — regardless of the size or shape of the decoded `Message`, at [1](#0-0) . The benchmark backing `WeightInfo::submit()` uses a single fixed fixture with no linear/`Vec`-length component [2](#0-1) [3](#0-2) .

### Title
Underpriced `submit` in `snowbridge-pallet-inbound-queue-v2` allows unbounded-size Ethereum messages to consume excessive weight/PoV during XCM conversion - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
This is a local analog of the CultureIndex report: a public entrypoint accepts attacker-controlled data whose size is not bounded or fee-metered, and that data is later expanded/processed (multiple times, in an unrelated code path) at a cost proportional to its size — enabling underpriced work that can degrade block production.

### Finding Description
`submit()` decodes an Ethereum event log into a `Message` struct that contains two attacker-controlled, unbounded fields: `assets: Vec<EthereumAsset>` and `payload: Payload::Raw(Vec<u8>)` [4](#0-3) . These originate from a Solidity event (`EthereumAsset[] assets`, `bytes data` for the XCM payload) emitted by the permissionless `v2_sendMessage`/gateway contract call on Ethereum, so any Ethereum-side caller controls their length [5](#0-4) .

After proof verification, `process_message` hands the message to `MessageProcessor::process_message`, which converts it to XCM via `MessageToXcm::convert` → `prepare()`. `prepare()` iterates `message.assets` unbounded, pushing one `AssetTransfer` per entry [6](#0-5) , and decodes/appends the entire raw XCM payload with `decode_raw_xcm` and `instructions.extend(message.remote_xcm.0)` [7](#0-6) [8](#0-7) . None of this scales the weight charged for `submit()`, which is the fixed constant `T::WeightInfo::submit()` [9](#0-8) .

Unlike the sibling `inbound-queue` (v1) pallet, which defines `type MaxMessageSize: Get<u32>` and charges `LengthToFee` for delivery cost estimation [10](#0-9) , the v2 `Config` for `inbound-queue-v2` has **no `MaxMessageSize` bound or length-based fee** at all [11](#0-10) . The only size restriction upstream is whatever the Ethereum EL/CL enforces on log/receipt size (bounded by Ethereum's own block gas limit), which is an off-chain, out-of-protocol assumption from the Substrate side, not an enforced on-chain limit.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing," an explicitly in-scope impact class. A relayer can submit a valid, verified Ethereum event containing a very large `assets` array and/or a very large `payload.Raw` bytes blob. Decoding, iterating, and converting this into an outbound XCM (which is further encoded and queued/dispatched into `XcmpQueue`) costs CPU/PoV proportional to the attacker-chosen size, but the extrinsic is charged the same fixed weight as a minimal one-asset message. This can cause:
- Actual execution time for `submit()` to exceed its declared weight, disrupting block production time budgets on BridgeHub.
- A resulting outbound XCM message large enough to itself stress the XCMP/message-queue path (though that path has its own size limits, the CPU cost of the conversion in `inbound-queue-v2` itself is already unmetered before that limit is reached).
- Repeated cheap submissions (fixed fee) each carrying maximal-size payloads, amplifying the effect across a block.

### Likelihood Explanation
No malicious peer/validator/relayer collusion is required beyond a single self-interested actor calling the permissionless `v2_sendMessage` on the Ethereum Gateway (an ordinary user action) and then acting as (or paying) a relayer to call `submit`. The vulnerable code path (`prepare()`/`convert()`) is reached on every successfully verified message, so the trigger condition is simply "large event payload," which is entirely within an ordinary user's control on the Ethereum side.

### Recommendation
- Add a `MaxMessageSize`/`MaxAssetsPerMessage` bound to `snowbridge-pallet-inbound-queue-v2::Config`, matching the pattern already used in `inbound-queue` (v1).
- Enforce these bounds in `Message::try_from`/`prepare()` (reject or truncate before conversion) rather than relying on Ethereum-side gas limits.
- Make `WeightInfo::submit()` a function of the decoded message size (e.g., `submit(len: u32, num_assets: u32)`), and benchmark/charge accordingly, mirroring how `pallet-preimage::note_preimage(s)` and `pallet-contracts::upload_code(c)` scale weight with declared input length.

### Proof of Concept
1. On Ethereum, call the Gateway's `v2_sendMessage` with a `Payload` containing `assets` populated with the maximum array length permitted by the EVM call itself (thousands of entries) and/or a very large `xcm.data` byte blob, keeping the whole transaction within Ethereum's own gas/calldata limits (no restriction from the Substrate side).
2. Wait for the transaction to be included in a finalized Ethereum block; obtain the receipt/log proof.
3. Submit `EthereumInboundQueueV2::submit(event_proof)` on BridgeHub. This extrinsic verifies the proof and is charged only the fixed `WeightInfo::submit()` weight.
4. Inside `process_message`, `MessageToXcm::convert` iterates the full `assets` vector and appends the full raw XCM payload into `instructions`, consuming CPU/PoV proportional to the attacker-chosen size — not reflected in the weight charged in step 3.
5. Repeating this with maximal-size messages at the fixed fee demonstrates underpriced, size-unbounded work being performed on every block that processes such a message.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L80-101)
```rust
	#[pallet::config]
	pub trait Config: frame_system::Config {
		#[allow(deprecated)]
		type RuntimeEvent: From<Event<Self>> + IsType<<Self as frame_system::Config>::RuntimeEvent>;
		/// The verifier for inbound messages from Ethereum.
		type Verifier: Verifier;
		/// Address of the Gateway contract.
		#[pallet::constant]
		type GatewayAddress: Get<H160>;
		/// Process the message that was submitted.
		type MessageProcessor: MessageProcessor<Self::AccountId>;
		#[cfg(feature = "runtime-benchmarks")]
		type Helper: BenchmarkHelper<Self>;
		/// Reward discriminator type.
		type RewardKind: Parameter + MaxEncodedLen + Send + Sync + Copy + Clone;
		/// The default RewardKind discriminator for rewards allocated to relayers from this pallet.
		#[pallet::constant]
		type DefaultRewardKind: Get<Self::RewardKind>;
		/// Relayer reward payment.
		type RewardPayment: RewardLedger<Self::AccountId, Self::RewardKind, u128>;
		type WeightInfo: WeightInfo;
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/benchmarking.rs (L10-29)
```rust
#[benchmarks]
mod benchmarks {
	use super::*;

	#[benchmark]
	fn submit() -> Result<(), BenchmarkError> {
		let caller: T::AccountId = whitelisted_caller();

		let create_message = T::Helper::initialize_storage();

		#[block]
		{
			assert_ok!(InboundQueue::<T>::submit(
				RawOrigin::Signed(caller.clone()).into(),
				Box::new(create_message.event),
			));
		}

		Ok(())
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/weights.rs (L18-35)
```rust
/// Weight functions needed for ethereum_beacon_client.
pub trait WeightInfo {
    fn submit() -> Weight;
}

// For backwards compatibility and tests
impl WeightInfo for () {
    fn submit() -> Weight {
        // Proof Size summary in bytes:
        //  Measured:  `309`
        //  Estimated: `3774`
        // Minimum execution time: 59_000_000 picoseconds.
        Weight::from_parts(60_000_000, 0)
            .saturating_add(Weight::from_parts(0, 3774))
            .saturating_add(RocksDbWeight::get().reads(7))
            .saturating_add(RocksDbWeight::get().writes(2))
    }
}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L16-49)
```rust
sol! {
	interface IGatewayV2 {
		struct AsNativeTokenERC20 {
			address token_id;
			uint128 value;
		}
		struct AsForeignTokenERC20 {
			bytes32 token_id;
			uint128 value;
		}
		struct EthereumAsset {
			uint8 kind;
			bytes data;
		}
		struct Xcm {
			uint8 kind;
			bytes data;
		}
		struct XcmCreateAsset {
			address token;
			uint8 network;
		}
		struct Payload {
			address origin;
			EthereumAsset[] assets;
			Xcm xcm;
			bytes claimer;
			uint128 value;
			uint128 executionFee;
			uint128 relayerFee;
		}
		event OutboundMessageAccepted(uint64 nonce, Payload payload);
	}
}
```

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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L167-200)
```rust
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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L327-342)
```rust
	/// Parse and (non-strictly) decode `raw` XCM bytes into a `Xcm<()>`.
	/// If decoding fails, return an empty `Xcm<()>`—thus allowing the message
	/// to proceed so assets can still be trapped on AH rather than the funds being locked on
	/// Ethereum but not accessible on AH.
	fn decode_raw_xcm(raw: &[u8]) -> Xcm<()> {
		let mut data = raw;
		if let Ok(versioned_xcm) =
			VersionedXcm::<()>::decode_with_depth_limit(MAX_XCM_DECODE_DEPTH, &mut data)
		{
			if let Ok(decoded_xcm) = versioned_xcm.try_into() {
				return decoded_xcm;
			}
		}
		// Decoding failed; allow an empty XCM so the message won't fail entirely.
		Xcm::new()
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L413-426)
```rust
		// If the message origin is not the gateway proxy contract, set the origin to
		// the original sender on Ethereum. Important to be before the arbitrary XCM that is
		// appended to the message on the next line.
		if message.origin != GatewayProxyAddress::get() {
			instructions.push(DescendOrigin(
				AccountKey20 { key: message.origin.into(), network: None }.into(),
			));
		}

		// Add the XCM sent in the message to the end of the xcm instruction
		instructions.extend(message.remote_xcm.0);

		Ok(instructions.into())
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L132-143)
```rust
		/// Convert a weight value into deductible balance type.
		type WeightToFee: WeightToFee<Balance = BalanceOf<Self>>;

		/// Convert a length value into deductible balance type
		type LengthToFee: WeightToFee<Balance = BalanceOf<Self>>;

		/// The upper limit here only used to estimate delivery cost
		type MaxMessageSize: Get<u32>;

		/// To withdraw and deposit an asset.
		type AssetTransactor: TransactAsset;
	}
```
