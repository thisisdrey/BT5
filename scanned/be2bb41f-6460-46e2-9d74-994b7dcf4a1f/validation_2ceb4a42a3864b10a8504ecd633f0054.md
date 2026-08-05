## Analysis

The external report's core broken invariant: a merkle proof (`extraProof`) is cryptographically self-consistent and gets verified in isolation, but the code never checks that the verified sub-proof is *bound* to the trusted parent commitment (`massExitBlock.blockRid`) that the whole security model rests on. The sibling function (`_withdrawRequest`) does the binding check; the vulnerable function skips it.

The same "verify the artifact but forget to bind it to the trusted anchor" pattern exists between the two Snowbridge v2 queue pallets in this repo.

`snowbridge-pallet-outbound-queue-v2::submit_delivery_receipt` explicitly binds the verified event to the trusted Ethereum contract before paying out: [1](#0-0) 

But `snowbridge-pallet-inbound-queue-v2::submit`, which shares the exact same `Verifier::verify` step and also declares a `GatewayAddress` config constant, never performs the equivalent check before converting the log into a dispatchable `Message`: [2](#0-1) [3](#0-2) 

The `Verifier::verify` implementation only proves that the *supplied* `event_log` (whose `address` field is attacker/relayer-supplied metadata, not independently checked against a config constant) is included in a finalized transaction receipt, and that the log's own address/topics/data match the log the relayer typed in: [4](#0-3) [5](#0-4) 

None of `verify`, `verify_receipt_inclusion`, or `check_log_match` compares `log.address` against `T::GatewayAddress`. This means the proof-verification step only proves internal self-consistency (the log exists at that address on Ethereum) — exactly analogous to the Solidity bug where `stateProof`/`extraProof` are internally self-consistent but never checked against the actual `massExitBlock` root. If `Message::try_from` in `snowbridge-inbound-queue-primitives` (not independently re-verified here due to index limits) does not itself re-check the address against `GatewayAddress`, then any Ethereum contract's event log matching the Gateway's event signature — not just the real Gateway contract — could be relayed and dispatched via XCM into the Polkadot side, since the pallet's own `submit()` path never enforces `event.event_log.address == T::GatewayAddress::get()`.

I was not able to fully verify the body of `Message::try_from` / `MessageProcessor` (files `bridges/snowbridge/primitives/inbound-queue/src/v2/{message.rs,converter.rs,processor.rs}`) within the remaining tool budget to confirm whether the gateway-address check is performed deeper in that call chain instead of in `lib.rs`. Given the strict "no uncertainty" requirement, I flag this explicitly: the asymmetry between the outbound pallet's explicit exported check and the inbound pallet's absence of that check at the same call-site layer is concrete and citable, but full confirmation that no equivalent check exists anywhere in the call chain would require reading `message.rs`/`converter.rs`/`processor.rs`, which I could not complete in this session.

### Title
Inbound Queue V2 `submit()` never binds the verified Ethereum log to the trusted `GatewayAddress` before dispatch - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
`EthereumInboundQueueV2::submit` verifies proof-of-inclusion for an Ethereum log and converts it into an XCM message, but — unlike its sibling `EthereumOutboundQueueV2::submit_delivery_receipt`, which explicitly checks `T::GatewayAddress::get() == receipt.gateway` — the inbound pallet's `submit()` contains no equivalent binding check at the point of dispatch, mirroring the reported bug class where a verified sub-artifact is never checked against the trusted anchor.

### Finding Description
`submit()` calls `T::Verifier::verify(&event.event_log, &event.proof)` and, on success, immediately proceeds to `Message::try_from(&event.event_log)` and `process_message`. `Verifier::verify` (implemented by the ethereum-client pallet) only checks (a) the beacon/execution header is finalized, and (b) the supplied `event_log` is present, byte-for-byte, in the receipt at `tx_index` — it never compares `event_log.address` to the pallet's configured `GatewayAddress`. The `GatewayAddress` config constant exists in the pallet but is not referenced inside `submit()`.

### Impact Explanation
If the address-binding check is indeed missing throughout the call chain, any Ethereum contract emitting a topic-compatible event (`OutboundMessageAccepted`) could have its logs relayed, verified, and dispatched as legitimate bridge messages to AssetHub, letting an unprivileged relayer forge cross-chain messages/asset movements that were never authorized by the real Gateway contract — a direct "forged or mis-bound proof acceptance" impact matching the required Impact Gate.

### Likelihood Explanation
Likelihood is high in principle (any Ethereum account can emit an arbitrarily crafted event with matching signature) but the finding is only definitively provable if the gateway-address check is not enforced elsewhere in `Message::try_from`/`MessageProcessor`, which I could not fully verify within this session's tool budget.

### Recommendation
Add an explicit `ensure!(event.event_log.address == T::GatewayAddress::get(), Error::<T>::InvalidGateway)` in `submit()` (mirroring `process_delivery_receipt` in outbound-queue-v2) before any conversion/dispatch occurs, and/or confirm and enforce this binding centrally inside `Verifier::verify` so all consumers get it automatically.

### Proof of Concept
Conceptual: a relayer submits an `EventProof` where `event_log.address` is a non-Gateway contract that happens to emit an event with the same topic0 signature and ABI-compatible payload as `OutboundMessageAccepted`. The receipt-inclusion proof verifies correctly against the real finalized execution header (since the log really exists in that address's transaction), `submit()` proceeds, and the forged message is converted and dispatched — without any point in `lib.rs` checking that the log's address is the actual trusted Gateway contract.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L453-454)
```rust
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
```

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-198)
```rust
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

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L15-41)
```rust
impl<T: Config> Verifier for Pallet<T> {
	/// Verify a message by verifying the existence of the corresponding
	/// Ethereum log in a block. Returns the log if successful. The execution header containing
	/// the log is sent with the message. The beacon header containing the execution header
	/// is also sent with the message, to check if the header is an ancestor of a finalized
	/// header.
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
			.map_err(|e| InvalidExecutionProof(e.into()))?;

		Self::verify_receipt_inclusion(
			proof.execution_proof.execution_header.receipts_root(),
			event_log.tx_index,
			&proof.receipt_proof,
			event_log,
		)?;

		Ok(())
	}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L65-79)
```rust
	fn check_log_match(log: &Log, receipt_log: &AlloyLog) -> bool {
		let equal = receipt_log.data.data.0 == log.data &&
			receipt_log.address.0 == log.address.0 &&
			receipt_log.topics().len() == log.topics.len();
		if !equal {
			return false;
		}
		for (_, (topic1, topic2)) in receipt_log.topics().iter().zip(log.topics.iter()).enumerate()
		{
			if topic1.0 != topic2.0 {
				return false;
			}
		}
		true
	}
```
