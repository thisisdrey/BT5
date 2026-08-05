This confirms the core mechanism: every `#[pallet::call]` dispatchable is automatically wrapped in `frame_support::storage::with_storage_layer`, which rolls back all storage changes (including the `Nonce<T>` increment) if the function returns `Err` [1](#0-0) [2](#0-1) . This validates the claim's core mechanism that a failing `send_xcm` call at the end of `submit` rolls back the nonce increment performed earlier via `try_mutate` [3](#0-2) [4](#0-3) .

I also confirmed that `XcmpQueue::deliver` and `validate` (the underlying `SendXcm` implementation typically wired via `T::XcmSender`) can legitimately fail for non-malicious, operational reasons — e.g., `MessageSendError::TooBig`/`NoChannel` mapped to `SendError::Transport`, or `DestinationUnsupported` from version-negotiation failure — none of which require a malicious peer [5](#0-4) [6](#0-5) . Channel suspension is a normal, automatic congestion-management mechanism triggered by queue depth thresholds, not malicious behavior [7](#0-6) .

The strict sequential nonce check (`envelope.nonce != nonce.saturating_add(1)` → `InvalidNonce`) is the only path that advances `Nonce<T>`, confirming there is no bypass/skip mechanism in this pallet [8](#0-7) .

All elements of the claim check out against the actual code: the atomic rollback mechanism, the strict sequential nonce gating, the realistic non-malicious triggers for `send_xcm` failure, and the absence of any bypass path in `snowbridge-pallet-inbound-queue` V1 (unlike `pallet-message-queue`'s per-message isolation). The claim is well-supported by the code.

Audit Report

## Title
Permanent per-channel stall in Snowbridge V1 Inbound Queue when downstream XCM send fails — no bypass, funds/messages permanently blocked - (File: `bridges/snowbridge/pallets/inbound-queue/src/lib.rs`)

## Summary
`Pallet::submit` in `bridges/snowbridge/pallets/inbound-queue/src/lib.rs` performs strict sequential nonce validation, fee burning, and `send_xcm` dispatch all within a single dispatchable call that FRAME automatically wraps in a storage transaction. If `send_xcm` fails (e.g., due to legitimate XCMP/HRMP channel congestion, suspension, or size/version mismatch), the entire call — including the `Nonce<T>` increment — is rolled back, and because nonces must be strictly sequential with no bypass, the channel is stalled indefinitely until the underlying delivery condition resolves.

## Finding Description
`submit` decodes and verifies the Ethereum event, then advances the per-channel nonce via `Nonce::<T>::try_mutate`, requiring `envelope.nonce == nonce + 1` or rejecting with `InvalidNonce` [9](#0-8) . Later in the same call, after burning fees, it dispatches the converted XCM via `Self::send_xcm`, which wraps `send_xcm::<T::XcmSender>` and maps any `XcmpSendError` into `Error::<T>::Send` [10](#0-9) [11](#0-10) [12](#0-11) .

Every `#[pallet::call]` dispatchable is macro-wrapped in `frame_support::storage::with_storage_layer`, which rolls back all storage writes performed during the call if it returns `Err` [1](#0-0) [2](#0-1) . Consequently, a `send_xcm` failure at the tail of `submit` rolls back the earlier `Nonce<T>` increment, leaving the channel's nonce unchanged.

The underlying `T::XcmSender` (typically `cumulus_pallet_xcmp_queue::Pallet` implementing `SendXcm`) can legitimately fail for non-malicious, operational reasons: `deliver` maps `send_fragment` errors (e.g., `MessageSendError::TooBig`, `NoChannel`) into `SendError::Transport` [5](#0-4) , and `validate` can return `SendError::DestinationUnsupported` if version negotiation fails, or `SendError::ExceedsMaxMessageSize` if the encoded message isn't decodable [13](#0-12) . Channel suspension due to congestion is an automatic, non-malicious mechanism triggered purely by queue depth thresholds on the receiving side [7](#0-6) .

Because `Nonce<T>` only ever advances via the strict `try_mutate` check requiring an exact +1 increment, and this is gated on the entire `submit` call (including the downstream `send_xcm`) succeeding, there is no code path in this pallet that allows the nonce to advance, retry independently, or skip a stuck message. This is architecturally different from `pallet-message-queue`, which processes each message in its own `with_transaction` scope and explicitly distinguishes permanent failures (dropped with an event) from transient ones (retried later without blocking the whole queue) [14](#0-13) .

## Impact Explanation
Any relayer-submitted event whose destination XCM delivery permanently or persistently fails (e.g., suspended/full HRMP channel, oversized/misconfigured message, unsupported XCM version at destination) causes `Nonce<T>` for that `ChannelId` to remain stuck. Every subsequent, otherwise-valid message for that channel is then rejected by the `InvalidNonce` check regardless of validity, permanently blocking all further inbound bridge traffic (including asset teleports) through that channel until external/governance intervention resolves the routing issue. This matches the "permanent user-fund or bridge-state lock" and "public underpriced work that ... stalls bridge processing" categories, since a single ordinary XCMP/HRMP condition (not requiring privileged or malicious action) suffices to freeze the entire channel indefinitely.

## Likelihood Explanation
The `submit` extrinsic is permissionless and callable by any signed relayer with a valid event proof; no privileged or malicious actor is required. XCMP/HRMP channel suspension due to queue congestion is a normal automatic mechanism [15](#0-14) , and message size/version mismatches are realistic operational/config conditions. Because the nonce check is strict and un-bypassable, the very first such failure is sufficient to freeze the channel — there is no probabilistic recovery without external intervention (e.g., waiting for the destination to drain its queue and resume, or a governance fix).

## Recommendation
Decouple nonce commitment from XCM delivery success — e.g., route inbound messages through `pallet-message-queue`-style per-message transactional isolation (as Snowbridge's V2 pallets already do) so a single message's delivery failure cannot block independent, subsequent messages. Alternatively, add an explicit skip/backfill mechanism for a stuck nonce, and emit a distinguishable failure event (analogous to `OverweightEnqueued`/`ProcessingFailed` in `pallet-message-queue`) so operators can detect and respond to a stalled channel without requiring the underlying transport condition to self-resolve.

## Proof of Concept
1. In the mock runtime (`bridges/snowbridge/pallets/inbound-queue/src/mock.rs`), configure `XcmSender` to return `XcmpSendError::Transport(_)` for a specific destination `para_id` (simulating a suspended/full HRMP channel or an oversized message).
2. Call `submit` with a valid, correctly-proven event for nonce `N+1` on that channel: the call fails with `Error::<T>::Send(SendError::Transport)`; assert `Nonce::<T>::get(channel_id) == N` (unchanged, confirming rollback of the earlier `try_mutate` increment).
3. Resubmit the identical event: it fails identically, since the mocked `XcmSender` condition is independent of pallet state.
4. Submit a different, otherwise-valid event with nonce `N+2` for the same channel: it is rejected with `Error::<T>::InvalidNonce`, confirming the channel is now permanently stalled until the `XcmSender` failure condition is externally resolved.

### Citations

**File:** substrate/frame/support/procedural/src/pallet/expand/call.rs (L240-246)
```rust
				method.block = syn::parse_quote! {{
					// We execute all dispatchable in a new storage layer, allowing them
					// to return an error at any point, and undoing any storage changes.
					#frame_support::storage::with_storage_layer::<#ok_type, #err_type, _>(
						|| #block
					)
				}};
```

**File:** substrate/frame/support/src/storage/transactional.rs (L188-201)
```rust
pub fn with_storage_layer<T, E, F>(f: F) -> Result<T, E>
where
	E: From<DispatchError>,
	F: FnOnce() -> Result<T, E>,
{
	with_transaction(|| {
		let r = f();
		if r.is_ok() {
			TransactionOutcome::Commit(r)
		} else {
			TransactionOutcome::Rollback(r)
		}
	})
}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L205-221)
```rust
	impl<T: Config> From<XcmpSendError> for Error<T> {
		fn from(e: XcmpSendError) -> Self {
			match e {
				XcmpSendError::NotApplicable => Error::<T>::Send(SendError::NotApplicable),
				XcmpSendError::Unroutable => Error::<T>::Send(SendError::NotRoutable),
				XcmpSendError::Transport(_) => Error::<T>::Send(SendError::Transport),
				XcmpSendError::DestinationUnsupported => {
					Error::<T>::Send(SendError::DestinationUnsupported)
				},
				XcmpSendError::ExceedsMaxMessageSize => {
					Error::<T>::Send(SendError::ExceedsMaxMessageSize)
				},
				XcmpSendError::MissingArgument => Error::<T>::Send(SendError::MissingArgument),
				XcmpSendError::Fees => Error::<T>::Send(SendError::Fees),
			}
		}
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L223-225)
```rust
	/// The current nonce for each channel
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageMap<_, Twox64Concat, ChannelId, u64, ValueQuery>;
```

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

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L297-311)
```rust
			// Burning fees for teleport
			Self::burn_fees(channel.para_id, fee)?;

			// Attempt to send XCM to a dest parachain
			let message_id = Self::send_xcm(xcm, channel.para_id)?;

			Self::deposit_event(Event::MessageReceived {
				channel_id: envelope.channel_id,
				nonce: envelope.nonce,
				message_id,
				fee_burned: fee,
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L337-341)
```rust
		pub fn send_xcm(xcm: Xcm<()>, dest: ParaId) -> Result<XcmHash, Error<T>> {
			let dest = Location::new(1, [Parachain(dest.into())]);
			let (xcm_hash, _) = send_xcm::<T::XcmSender>(dest, xcm).map_err(Error::<T>::from)?;
			Ok(xcm_hash)
		}
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L879-919)
```rust
impl<T: Config> OnQueueChanged<ParaId> for Pallet<T> {
	// Suspends/Resumes the queue when certain thresholds are reached.
	fn on_queue_changed(para: ParaId, fp: QueueFootprint) {
		let QueueConfigData { resume_threshold, suspend_threshold, .. } = <QueueConfig<T>>::get();

		let mut suspended_channels = <InboundXcmpSuspended<T>>::get();
		let suspended = suspended_channels.contains(&para);

		if suspended && fp.ready_pages <= resume_threshold {
			if let Err(err) = Self::send_signal(para, ChannelSignal::Resume) {
				tracing::error!(
					target: LOG_TARGET,
					error=?err,
					sibling=?para,
					"defensive: Could not send resumption signal to inbound channel of sibling; channel remains suspended."
				);
			} else {
				suspended_channels.remove(&para);
				<InboundXcmpSuspended<T>>::put(suspended_channels);
			}
		} else if !suspended && fp.ready_pages >= suspend_threshold {
			tracing::warn!(target: LOG_TARGET, sibling=?para, "XCMP queue for sibling is full; suspending channel.");

			if let Err(err) = Self::send_signal(para, ChannelSignal::Suspend) {
				// It will retry if `drop_threshold` is not reached, but it could be too late.
				tracing::error!(
					target: LOG_TARGET, error=?err,
					"defensive: Could not send suspension signal; future messages may be dropped."
				);
			} else if let Err(err) = suspended_channels.try_insert(para) {
				tracing::error!(
					target: LOG_TARGET,
					error=?err,
					sibling=?para,
					"Too many channels suspended; cannot suspend sibling; further messages may be dropped."
				);
			} else {
				<InboundXcmpSuspended<T>>::put(suspended_channels);
			}
		}
	}
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L1242-1260)
```rust
	fn validate(
		dest: &mut Option<Location>,
		msg: &mut Option<Xcm<()>>,
	) -> SendResult<(ParaId, VersionedXcm<()>)> {
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

				Ok(((id, versioned_xcm), price))
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L1271-1303)
```rust
	fn deliver((recipient, xcm): (ParaId, VersionedXcm<()>)) -> Result<XcmHash, SendError> {
		let hash = xcm.using_encoded(sp_io::hashing::blake2_256);

		let mut encoding = XcmEncoding::Simple;
		let mut all_channels = <OutboundXcmpStatus<T>>::get();
		if let Some(channel_details) = Self::try_get_outbound_channel(&mut all_channels, recipient)
		{
			if channel_details.flags.has_concatenated_opaque_versioned_xcm_support() {
				encoding = XcmEncoding::Double;
			}
		}

		let result = match encoding {
			XcmEncoding::Simple => {
				Self::send_fragment(recipient, XcmpMessageFormat::ConcatenatedVersionedXcm, xcm)
			},
			XcmEncoding::Double => Self::send_fragment(
				recipient,
				XcmpMessageFormat::ConcatenatedOpaqueVersionedXcm,
				xcm.encode(),
			),
		};
		match result {
			Ok(_) => {
				Self::deposit_event(Event::XcmpMessageSent { message_hash: hash });
				Ok(hash)
			},
			Err(e) => {
				tracing::error!(target: LOG_TARGET, error=?e, "Deliver error");
				Err(SendError::Transport(e.into()))
			},
		}
	}
```

**File:** substrate/frame/message-queue/src/lib.rs (L1569-1617)
```rust
		let transaction =
			storage::with_transaction(|| -> TransactionOutcome<Result<_, DispatchError>> {
				let res =
					T::MessageProcessor::process_message(message, origin.clone(), meter, &mut id);
				match &res {
					Ok(_) => TransactionOutcome::Commit(Ok(res)),
					Err(_) => TransactionOutcome::Rollback(Ok(res)),
				}
			});

		let transaction = match transaction {
			Ok(result) => result,
			_ => {
				defensive!(
					"Error occurred processing message, storage changes will be rolled back"
				);
				return MessageExecutionStatus::Unprocessable { permanent: true };
			},
		};

		match transaction {
			Err(Overweight(w)) if w.any_gt(overweight_limit) => {
				// Permanently overweight.
				Self::deposit_event(Event::<T>::OverweightEnqueued {
					id,
					origin,
					page_index,
					message_index,
				});
				MessageExecutionStatus::Overweight
			},
			Err(Overweight(_)) => {
				// Temporarily overweight - save progress and stop processing this
				// queue.
				MessageExecutionStatus::InsufficientWeight
			},
			Err(Yield) => {
				// Processing should be reattempted later.
				MessageExecutionStatus::Unprocessable { permanent: false }
			},
			Err(error @ BadFormat | error @ Corrupt | error @ Unsupported) => {
				// Permanent error - drop
				Self::deposit_event(Event::<T>::ProcessingFailed { id: id.into(), origin, error });
				MessageExecutionStatus::Unprocessable { permanent: true }
			},
			Err(error @ StackLimitReached) => {
				Self::deposit_event(Event::<T>::ProcessingFailed { id: id.into(), origin, error });
				MessageExecutionStatus::StackLimitReached
			},
```
