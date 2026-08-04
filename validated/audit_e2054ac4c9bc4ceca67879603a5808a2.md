### Title
Snowbridge inbound-queue-v2 accepts Ethereum messages in any nonce order, dropping the sequential-ordering guarantee enforced by v1 - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
`snowbridge-pallet-inbound-queue-v2::process_message` only checks that a given Ethereum message nonce has never been consumed before; it never checks that the nonce is the *next expected* one for the channel. This is a strict regression from the sibling v1 pallet, which enforces exact sequential nonce increment. As a result, XCM dispatch to AssetHub/downstream parachains derived from Ethereum-originated messages can be executed out of order — the exact bug class described in the external report (cross-chain messages processed out of order break dApps that rely on ordering, e.g. "stake" then "unstake").

### Finding Description
`Nonce<T>` in the v2 pallet is a `SparseBitmapImpl` over `NonceBitmap<T>`, purely an anti-replay set: [1](#0-0) 

The only check performed before accepting and dispatching a message is a replay check, not an ordering check: [2](#0-1) 

`SparseBitmapImpl::get/set` simply flips an arbitrary bit corresponding to the nonce value, with no concept of "last processed" or "next expected" nonce: [3](#0-2) 

This is in stark contrast with the v1 `inbound-queue` pallet, which stores a per-channel running `Nonce<T>` counter and rejects any submission whose nonce is not exactly `previous + 1`: [4](#0-3) 

Because `submit`/`process_message` in v2 is a permissionless, `ensure_signed`-only extrinsic (any account can act as relayer) and immediately triggers `T::MessageProcessor::process_message`, which converts the payload to XCM and forwards it to AssetHub/other parachains: [5](#0-4) [6](#0-5) 

there is nothing preventing nonce `N+1` from being submitted, verified, and dispatched to the destination chain before nonce `N`. Relayer submission order is not guaranteed to match Ethereum emission order: multiple concurrent relayers, differing beacon-client sync speed, transaction pool reordering within a Bridge Hub block, or even a single honest relayer racing two `submit` calls can all cause reordering — none of these require a "malicious relayer" assumption, matching the root cause identified in the external report (cross-chain observers/relayers are not synchronized, so finality/verification order does not match send order).

### Impact Explanation
Any dApp or bridging flow built on Snowbridge v2 that sends multiple Ethereum-originated messages that must be processed in a specific order (e.g., "register asset" then "transfer asset", "lock" then "mint", "deposit" then "withdraw", or a claimer/topic dependent sequence used in `snowbridge_v2_inbound` tests) can have its second message executed before the first. On the Polkadot side this manifests as:
- XCM execution failures on AssetHub/downstream parachains (holding-register traps, `AssetsTrapped` events, as seen in `fallback_claimer_traps_to_bridge_owner_and_claim_assets_succeeds`), requiring manual claim recovery.
- Silent divergence between intended application state and actual state (e.g., a swap or registration executed against stale/absent prerequisite state).
- Loss of relayer/user funds or fees when the dependent message fails after the relayer fee/tip has already been paid out (`T::RewardPayment::register_reward` runs unconditionally once `process_message` for that nonce succeeds internally, regardless of whether the dependent business logic is semantically valid given prior message state).

This aligns with the required impact categories: "runtime bugs that compromise intended behavior" and potential "permanent user-fund … lock" via trapped assets that require manual claim intervention.

### Likelihood Explanation
High likelihood in practice: `submit` is fully permissionless (`ensure_signed` only, no special relayer role), and nothing in the protocol or the pallet enforces submission order matches emission order. Any timing skew between relayers, beacon light-client verification races, or transaction-pool inclusion order in a Bridge Hub block is sufficient to trigger the reordering — no privileged/malicious actor is required, only realistic operational asynchrony that this repository's own v1 pallet was explicitly designed to prevent via strict nonce sequencing.

### Recommendation
Restore sequential ordering enforcement in `inbound-queue-v2`, mirroring the v1 design: track the last successfully processed nonce per relevant scope (e.g., per origin/channel) and reject (or buffer/re-queue) messages whose nonce is not the next expected value, instead of relying solely on the `SparseBitmapImpl` anti-replay check. If out-of-order acceptance is an intentional design tradeoff (e.g., for throughput), then downstream `MessageProcessor`/XCM converters must be made ordering-agnostic (no message content should assume prior-message state), and this assumption should be explicitly documented and tested for every use case that composes multiple sequential Ethereum-origin messages.

### Proof of Concept
1. Deploy `snowbridge-pallet-inbound-queue-v2` as configured in `bridge_to_ethereum_config.rs`.
2. Two Ethereum-side messages are emitted by the Gateway in order: nonce `1` = "register token" / prerequisite operation, nonce `2` = "use token" / dependent operation.
3. A relayer (or two independent relayers) submits nonce `2`'s `EventProof` via `submit` before nonce `1`'s proof reaches Bridge Hub (e.g., due to differing beacon-client header-availability timing).
4. `process_message` for nonce `2` only checks `!Nonce::<T>::get(2)` (per lines 214-245 of `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`) — this passes since nonce `2` was never seen before, regardless of nonce `1`'s status.
5. The XCM derived from message `2` is dispatched to AssetHub and fails/traps because its prerequisite (message `1`'s effect) has not yet landed — reproducing the "out-of-order cross-chain message" failure mode from the external report, entirely through the intended, permissionless `submit` flow, with no malicious actor required.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L165-168)
```rust
	/// StorageMap used for encoding a SparseBitmapImpl that tracks whether a specific nonce has
	/// been processed or not. Message nonces are unique and never repeated.
	#[pallet::storage]
	pub type NonceBitmap<T: Config> = StorageMap<_, Twox64Concat, u64, u128, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L180-198)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-245)
```rust
	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
```

**File:** bridges/snowbridge/primitives/core/src/sparse_bitmap.rs (L56-84)
```rust
impl<BitMap> SparseBitmap<BitMap> for SparseBitmapImpl<BitMap>
where
	BitMap: StorageMap<u64, u128, Query = u128>,
{
	/// Checks if the bit at the specified index is set.
	/// Returns `true` if the bit is set, `false` otherwise.
	/// * `index`: The index (nonce) to check.
	fn get(index: u64) -> bool {
		// Calculate bucket and mask
		let (bucket, mask) = Self::compute_bucket_and_mask(index);

		// Retrieve bucket and check bit
		let bucket_value = BitMap::get(bucket);
		bucket_value & mask != 0
	}

	/// Sets the bit at the specified index.
	/// This marks the nonce as processed by setting its corresponding bit in the bitmap.
	/// * `index`: The index (nonce) to set.
	fn set(index: u64) {
		// Calculate bucket and mask
		let (bucket, mask) = Self::compute_bucket_and_mask(index);

		// Mutate the storage to set the bit
		BitMap::mutate(bucket, |value| {
			*value |= mask; // Set the bit in the bucket
		});
	}
}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L223-267)
```rust
	/// The current nonce for each channel
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageMap<_, Twox64Concat, ChannelId, u64, ValueQuery>;

	/// The current operating mode of the pallet.
	#[pallet::storage]
	#[pallet::getter(fn operating_mode)]
	pub type OperatingMode<T: Config> = StorageValue<_, BasicOperatingMode, ValueQuery>;

	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Submit an inbound message originating from the Gateway contract on Ethereum
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::submit())]
		pub fn submit(origin: OriginFor<T>, event: EventProof) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into an Envelope
			let envelope =
				Envelope::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidEnvelope)?;

			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == envelope.gateway, Error::<T>::InvalidGateway);

			// Retrieve the registered channel for this message
			let channel =
				T::ChannelLookup::lookup(envelope.channel_id).ok_or(Error::<T>::InvalidChannel)?;

			// Verify message nonce
			<Nonce<T>>::try_mutate(envelope.channel_id, |nonce| -> DispatchResult {
				if *nonce == u64::MAX {
					return Err(Error::<T>::MaxNonceReached.into());
				}
				if envelope.nonce != nonce.saturating_add(1) {
					Err(Error::<T>::InvalidNonce.into())
				} else {
					*nonce = nonce.saturating_add(1);
					Ok(())
				}
			})?;
```
