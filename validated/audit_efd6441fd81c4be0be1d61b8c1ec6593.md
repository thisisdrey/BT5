## Title
Permanent channel lockout via strict sequential-nonce enforcement combined with atomic dispatchable rollback in Snowbridge Inbound Queue (V1) - (File: `bridges/snowbridge/pallets/inbound-queue/src/lib.rs`)

### Summary
The Ethereum→Polkadot inbound queue pallet enforces a strictly monotonic nonce (`nonce == last_nonce + 1`) per channel before any message can be accepted, and every FRAME dispatchable (including `submit`) is automatically wrapped in `with_storage_layer`, so *any* error occurring anywhere in `submit` (verification, `MessageConverter::convert`, `send_xcm`, fee burning) rolls back **all** storage writes for that call, including the nonce advance. If an attacker can get the Ethereum Gateway to emit an `OutboundMessageAccepted` event for a message whose payload decodes successfully but is crafted so that XCM conversion or dispatch deterministically fails (e.g. exceeds `SendXcm`'s max message size, or targets an unroutable/unsupported destination), that specific nonce can *never* be advanced past. Since nonce checking is strictly sequential, every legitimately queued message with a higher nonce becomes permanently undeliverable — mirroring the RToken bug class where one misbehaving asset locks 100% of an otherwise-healthy basket.

### Finding Description
`Pallet::submit` in `bridges/snowbridge/pallets/inbound-queue/src/lib.rs` performs, in order: verifier check, envelope decode, gateway check, then a **strict nonce check**: [1](#0-0) 

only after which it decodes the payload, converts it to XCM via `do_convert`, burns fees, and calls `send_xcm`: [2](#0-1) 

`Error<T>` explicitly maps `SendXcm` failures such as `ExceedsMaxMessageSize`, `DestinationUnsupported`, and `Unroutable` into pallet errors that abort the call: [3](#0-2) 

Every `#[pallet::call]` method, including `submit`, is compiled with an automatic `with_storage_layer` wrapper by the `pallet` macro, meaning any `Err` return unwinds **all** storage mutations performed during that call: [4](#0-3) [5](#0-4) 

Because the nonce mutation happens inside the same call before the later, potentially-failing steps, a message whose XCM conversion or `send_xcm` call *deterministically* fails will never be able to commit — the `Nonce<T>` storage for that channel is rolled back to its previous value every time it's retried, and the strict `nonce.saturating_add(1)` equality check means no message with a higher nonce can ever be accepted afterward. There is no permissionless (or even governance) mechanism in this pallet to skip a stuck nonce or force-advance it; `set_operating_mode` can only halt/resume the *entire* pallet, not fix ordering.

This is the direct structural analog of the reported RToken issue: a single message ("collateral asset") that is guaranteed to fail on interaction permanently blocks the processing pipeline ("`AssetRegistry.refresh()`") for everything downstream, even though those other items ("99% of collateral") are perfectly healthy.

### Impact Explanation
This matches the required impact class "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" and "permanent... bridge-state lock." A single crafted-but-otherwise-valid Ethereum message can permanently stall an entire bridge channel/lane (one Ethereum Gateway → one destination parachain), preventing delivery of all subsequent legitimate bridged assets/messages, freezing relayer rewards for that channel, and requiring privileged/off-chain (potentially not even possible) remediation since there is no on-chain nonce-skip call.

### Likelihood Explanation
The trigger does not require a malicious relayer, validator, or governance actor — only the ability to get the Gateway contract on Ethereum to emit `OutboundMessageAccepted` with a payload that: (1) decodes into a valid `VersionedMessage` (passes `decode_all`), (2) converts to a valid `Xcm<()>` via `MessageConverter`, but (3) is guaranteed to fail delivery via `send_xcm` (e.g., a payload sized to exceed the destination channel's max XCMP message size, or targeting an unsupported/unroutable destination). Any user permitted to call the Gateway's message-sending entry point on Ethereum (a permissionless bridge-user action, not a privileged relayer/validator role) can trigger this. Whether the specific Gateway contract restricts payload construction enough to prevent crafting such a message could not be fully verified from this repository alone (the Solidity Gateway contract source is out of scope here), so this should be validated against the actual Gateway constraints before treating it as fully weaponizable — but the on-chain pallet logic itself provides no defense once such an event is emitted and proven.

### Recommendation
- Decouple nonce/replay-protection bookkeeping from downstream processing outcomes: mark the nonce as "received" (to prevent replay) in a way that is not rolled back even if downstream XCM conversion/dispatch fails, and route processing failures to a recoverable dead-letter/parking state instead of aborting the whole call.
- Provide a governance-gated (or automatic, weight-metered) mechanism to explicitly skip/park a nonce that deterministically fails XCM dispatch, so subsequent nonces are not permanently blocked, analogous to `pallet-message-queue`'s `execute_overweight`/permanent-unprocessable handling.
- Add explicit test coverage for a message that passes verification/decoding but is guaranteed to fail at `send_xcm` (e.g., oversized payload), asserting subsequent nonces remain deliverable after remediation.

### Proof of Concept
1. Attacker calls the Ethereum Gateway's message-send entry point with a payload that decodes into a valid `VersionedMessage` but whose XCM conversion output is sized/structured so that `send_xcm` will always return `SendXcm::Unroutable` or `ExceedsMaxMessageSize` for the destination parachain's channel (e.g., an oversized/complex payload).
2. Relayer submits the proof via `submit(origin, event)` for nonce `N` (the next expected nonce for the channel).
3. `submit` passes verification, decode, and the nonce check (`Nonce<T>` mutated to `N` inside the call), then fails at `Self::send_xcm(...)?` returning `Error::<T>::Send(..)`.
4. Because `submit` is wrapped in `with_storage_layer`, the entire call — including the `Nonce<T>` mutation to `N` — is rolled back; on-chain state shows `Nonce<T>` still at `N-1`.
5. Any relayer retries submitting the same proof (nonce `N`) — it fails identically every time since the payload is fixed and deterministic (proven event data cannot be altered).
6. All messages with nonce `> N` sent afterward from Ethereum are permanently rejected by `<Nonce<T>>::try_mutate` with `Error::<T>::InvalidNonce`, since the channel's nonce can never advance past `N-1`, permanently halting the channel until a runtime upgrade or hard fork intervenes.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L192-221)
```rust
	#[derive(
		Clone, Encode, Decode, DecodeWithMemTracking, Eq, PartialEq, Debug, TypeInfo, PalletError,
	)]
	pub enum SendError {
		NotApplicable,
		NotRoutable,
		Transport,
		DestinationUnsupported,
		ExceedsMaxMessageSize,
		MissingArgument,
		Fees,
	}

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

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L283-311)
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

			Self::deposit_event(Event::MessageReceived {
				channel_id: envelope.channel_id,
				nonce: envelope.nonce,
				message_id,
				fee_burned: fee,
			});

			Ok(())
		}
```

**File:** substrate/frame/support/procedural/src/pallet/expand/call.rs (L239-246)
```rust
				let block = &method.block;
				method.block = syn::parse_quote! {{
					// We execute all dispatchable in a new storage layer, allowing them
					// to return an error at any point, and undoing any storage changes.
					#frame_support::storage::with_storage_layer::<#ok_type, #err_type, _>(
						|| #block
					)
				}};
```

**File:** substrate/frame/support/src/storage/transactional.rs (L183-201)
```rust
/// Execute the supplied function, adding a new storage layer.
///
/// This is the same as `with_transaction`, but assuming that any function returning an `Err` should
/// rollback, and any function returning `Ok` should commit. This provides a cleaner API to the
/// developer who wants this behavior.
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
