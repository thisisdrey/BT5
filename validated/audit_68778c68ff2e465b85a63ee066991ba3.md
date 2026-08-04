## Title
Snowbridge `inbound-queue` pallet permanently deadlocks a channel when an oversized Ethereum message cannot fit into the destination XCMP page, because nonce ordering is strict and there is no payload size cap - ([File: bridges/snowbridge/pallets/inbound-queue/src/lib.rs])

### Summary
The Snowbridge `inbound-queue` pallet enforces strict per-channel nonce ordering identical in spirit to the OPinit `MsgFinalizeTokenDeposit` sequencing bug: a message can only be accepted if `envelope.nonce == nonce + 1`. The nonce increment and the XCM conversion/dispatch happen inside the same dispatchable, and the payload size coming from the Ethereum `OutboundMessageAccepted` event is never bounded against the destination HRMP/XCMP channel's `max_message_size`. If a message's payload turns into an XCM that exceeds the channel's page/message size limit, `send_xcm` returns `Err(SendError::ExceedsMaxMessageSize)`, the whole extrinsic (and the nonce write) reverts, and because the offending nonce is immutable (assigned once on Ethereum), every future relay for that channel is stuck trying to replay the exact same, always-failing nonce — permanently freezing the bridge channel.

### Finding Description
`Pallet::submit` in `bridges/snowbridge/pallets/inbound-queue/src/lib.rs` requires `envelope.nonce == nonce.saturating_add(1)` for the per-channel `Nonce<T>` map, exactly as `x/opchild`'s `FinalizeTokenDeposit` required `req.Sequence == finalizedL1Sequence`: [1](#0-0) 

After the nonce check succeeds, the pallet decodes the untrusted `envelope.payload` into a `VersionedMessage`, converts it into an XCM via `T::MessageConverter`, and attempts delivery with `send_xcm`: [2](#0-1) 

`T::MaxMessageSize` exists but is only used to *estimate delivery cost/fees* — it is never used to reject an oversized envelope before it enters the nonce sequence: [3](#0-2) [4](#0-3) 

Once the derived XCM is handed to `XcmpQueue::deliver`, it is size-checked against the actual HRMP channel's `max_message_size`/`MaxPageSize`, and rejected with `SendError::ExceedsMaxMessageSize`/`MessageSendError::TooBig` if too large: [5](#0-4) [6](#0-5) 

Because a `submit` dispatchable's storage effects (including the `Nonce<T>` write) are all rolled back when the call returns `Err`, the nonce is *not* advanced when `send_xcm` fails late in the function. But the failing envelope originates from an immutable Ethereum event with a fixed nonce — the relayer cannot skip it, and the strict `nonce == stored+1` check means no other message for that channel can ever be accepted again. This mirrors the OPinit bug precisely: an unbounded, attacker-influenced payload combined with strict sequential processing turns one bad message into a permanent block of the entire channel.

### Impact Explanation
Any Ethereum-side caller able to trigger emission of an `OutboundMessageAccepted` event with an oversized `payload` (e.g. via a Gateway function that lets users attach large auxiliary data/instructions for the destination XCM, analogous to `MsgInitiateTokenDeposit.Data`) can produce a message whose resulting XCM exceeds the destination channel's `max_message_size`. Once minted on Ethereum, that nonce is permanent and unskippable. Every subsequent inbound message on that channel — including all further token deposits/XCM transfers from Ethereum to that parachain — becomes permanently unprocessable, a bridge-wide DoS/fund-lock affecting an unprivileged victim population, matching the "permanent bridge-state lock" / "public underpriced work that stalls bridge processing" impact class.

### Likelihood Explanation
No governance/admin/relayer misbehavior is required — a single unprivileged Ethereum-side interaction with a sufficiently large payload is sufficient. The only bound on the payload before it is bound to a nonce and enters the ordered lane is Ethereum log/gas limits, which are far larger than typical HRMP `max_message_size` configuration (commonly around 100 KB, per `MaxPageSize` usage referenced in `prdoc/1.13.0/pr_3952.prdoc`). There is no on-chain validation rejecting the envelope before nonce assignment, and no recovery extrinsic to force-skip a bad nonce in this pallet.

### Recommendation
- Enforce a hard cap on `envelope.payload` length (and/or on the resulting XCM's encoded size) against `T::MaxMessageSize`/destination channel limits *before* the nonce check/advance in `submit`, rejecting oversized envelopes deterministically without ever consuming a nonce slot.
- Provide a privileged (root-gated) recovery path to force-advance/skip a permanently unprocessable nonce, so a bridge channel cannot be frozen forever by a single malformed message.
- Consider validating the prospective XCM's `check_is_decodable()`/size against the live `ChannelInfo::max_message_size` prior to committing to the sequential nonce, mirroring the mitigation applied for `MsgInitiateTokenDeposit.Data` in OPinit.

### Proof of Concept
1. On Ethereum, trigger the Gateway to emit `OutboundMessageAccepted` with `nonce = N` and a `payload` whose XCM conversion (via `T::MessageConverter`) yields an XCM instruction set encoding to more bytes than the destination parachain's HRMP `max_message_size` (e.g., a large `Transact`/asset instruction list).
2. Relay this message with `InboundQueue::submit`. The pallet decodes the payload and calls `Self::send_xcm(xcm, channel.para_id)`, which internally calls `XcmpQueue::deliver` → `send_fragment`, returning `MessageSendError::TooBig` → `SendError::ExceedsMaxMessageSize` (see `cumulus/pallets/xcmp-queue/src/lib.rs:575-585`).
3. `submit` returns `Err(Error::<T>::Send(SendError::ExceedsMaxMessageSize))`; the whole extrinsic — including the earlier `Nonce<T>` write for `channel_id` — is rolled back.
4. Any legitimate message with `nonce = N` (the fixed Ethereum-assigned nonce) will always trigger this identical failure since payload and destination channel limits are unchanged. Because `Nonce<T>` never advances past `N-1`, no message with `nonce > N` can ever pass the `envelope.nonce != nonce.saturating_add(1)` check in `bridges/snowbridge/pallets/inbound-queue/src/lib.rs:261`, permanently blocking that channel.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L256-267)
```rust
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

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L283-301)
```rust
			// Decode payload into `VersionedMessage`
			let message = VersionedMessage::decode_all(&mut envelope.payload.as_ref())
				.map_err(|_| Error::<T>::InvalidPayload)?;

			// Decode message into XCM
			let (xcm, fee) = Self::do_convert(envelope.message_id, message.clone())?;

			tracing::info!(
				target: LOG_TARGET,
				?xcm,
				?fee,
				"💫 xcm decoded"
			);

			// Burning fees for teleport
			Self::burn_fees(channel.para_id, fee)?;

			// Attempt to send XCM to a dest parachain
			let message_id = Self::send_xcm(xcm, channel.para_id)?;
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L343-349)
```rust
		pub fn calculate_delivery_cost(length: u32) -> BalanceOf<T> {
			let weight_fee = T::WeightToFee::weight_to_fee(&T::WeightInfo::submit());
			let len_fee = T::LengthToFee::weight_to_fee(&Weight::from_parts(length as u64, 0));
			weight_fee
				.saturating_add(len_fee)
				.saturating_add(T::PricingParameters::get().rewards.local)
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L378-384)
```rust
	/// API for accessing the delivery cost of a message
	impl<T: Config> Get<BalanceOf<T>> for Pallet<T> {
		fn get() -> BalanceOf<T> {
			// Cost here based on MaxMessagePayloadSize(the worst case)
			Self::calculate_delivery_cost(T::MaxMessageSize::get())
		}
	}
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L569-585)
```rust
		// Optimization note: `max_message_size` could potentially be stored in
		// `OutboundXcmpMessages` once known; that way it's only accessed when a new page is needed.

		let channel_info =
			T::ChannelInfo::get_channel_info(recipient).ok_or(MessageSendError::NoChannel)?;
		// Max message size refers to aggregates, or pages. Not to individual fragments.
		let max_message_size = channel_info.max_message_size.min(T::MaxPageSize::get()) as usize;
		let format_size = format.encoded_size();
		// We check the encoded fragment length plus the format size against the max message size
		// because the format is concatenated if a new page is needed.
		let size_to_check = encoded_fragment
			.len()
			.checked_add(format_size)
			.ok_or(MessageSendError::TooBig)?;
		if size_to_check > max_message_size {
			return Err(MessageSendError::TooBig);
		}
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L1246-1258)
```rust
		let d = dest.take().ok_or(SendError::MissingArgument)?;

		match d.unpack() {
			// An HRMP message for a sibling parachain.
			(1, [Parachain(id)]) => {
				let xcm = msg.take().ok_or(SendError::MissingArgument)?;
				let id = ParaId::from(*id);
				let price = T::PriceForSiblingDelivery::price_for_delivery(id, &xcm);
				let versioned_xcm = T::VersionWrapper::wrap_version(&d, xcm)
					.map_err(|()| SendError::DestinationUnsupported)?;
				versioned_xcm
					.check_is_decodable()
					.map_err(|()| SendError::ExceedsMaxMessageSize)?;
```
