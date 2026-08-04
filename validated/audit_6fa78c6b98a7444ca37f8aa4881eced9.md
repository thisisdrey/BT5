## Analysis

The GMX report's core broken invariant is: **a status/sequence machine advances only on success of an external transfer, and a permanently-failing transfer (triggered by ordinary, non-privileged user action) leaves the machine stuck forever with no skip mechanism**, causing a total DoS until emergency governance intervenes.

The closest local analog is the Snowbridge `inbound-queue` (v1) pallet's strict per-channel nonce sequencing, which lacks a "permanently failing message" skip/reset mechanism (unlike `pallet-message-queue`, which explicitly handles this case via `OverweightEnqueued`/`execute_overweight`).

### Title
Snowbridge inbound-queue-v1 channel permanently stalls on a deterministically-failing message due to strict sequential nonce with no skip path - (File: `bridges/snowbridge/pallets/inbound-queue/src/lib.rs`)

### Summary
`Pallet::submit` in the Snowbridge V1 inbound queue enforces strict, gapless nonce incrementing per channel and only commits the nonce advance atomically together with message conversion (`do_convert`) and XCM dispatch (`send_xcm`). If any message with nonce `N` deterministically fails conversion or sending, the whole extrinsic reverts (including the nonce bump), so nonce `N` never advances — and because subsequent messages must have `nonce == N+1`, every future message on that channel becomes permanently unprocessable.

### Finding Description
In `submit()`: [1](#0-0) 
the nonce check requires `envelope.nonce == nonce.saturating_add(1)`, i.e. strictly gapless. This nonce mutation happens in the *same* dispatchable as message conversion and XCM sending: [2](#0-1) 

`do_convert` calls `T::MessageConverter::convert`, which can return `ConvertMessageError::InvalidToken`, `CannotReanchor`, or `InvalidDestination` deterministically based on message content (e.g. `convert_send_native_token`'s reanchor step): [3](#0-2) 
and `send_xcm` can permanently fail with `SendError::ExceedsMaxMessageSize` or `NotRoutable`/`DestinationUnsupported` for a given payload/route: [4](#0-3) 

Since the entire `submit()` call is one FRAME dispatch, any `Err` return causes the whole storage transaction (including the `Nonce` bump) to roll back. Any relayer resubmitting the exact same proof for nonce `N` will hit the identical deterministic failure every time — there is no mechanism analogous to `pallet-message-queue`'s permanent-overweight skip: [5](#0-4) 
to bypass message `N` and let `N+1, N+2, ...` proceed. The channel is stuck exactly like the GMX vault's `Status` stuck on a reverting transfer — no code path exists to advance past the poison message except a root-only `set_operating_mode` halt, which stops the whole pallet rather than resolving the stuck channel.

### Impact Explanation
Because the Snowbridge Ethereum→Polkadot bridge is exposed via a permissionless `submit` extrinsic and used to move real bridged assets (ETH, ERC-20s, PNAs) to AssetHub, a single deterministically-unprocessable message permanently halts delivery of **all subsequent messages on that channel**, i.e. all further Ethereum→AssetHub transfers for that channel are frozen. This matches "permanent user-fund or bridge-state lock" / "public underpriced work that ... stalls bridge processing" in the impact gate, since there is no automatic recovery — only manual/governance intervention (root `set_operating_mode`) which halts the entire pallet rather than unblocking the channel.

### Likelihood Explanation
The trigger does not require a malicious relayer, validator, or governance actor — any user who can get a transaction accepted by the Ethereum Gateway contract (permissionless) can craft a message whose payload deterministically fails XCM conversion/reanchoring or exceeds the destination's max XCM message size. Because the failure is deterministic on message content (not relayer behavior), no amount of resubmission attempts by honest relayers can unblock the channel, satisfying the "public entrypoint, unprivileged attacker" requirement.

### Recommendation
Decouple nonce advancement from downstream conversion/dispatch success: increment/mark the nonce as *consumed* independent of whether conversion or XCM-send succeeds, and route failures (conversion/send errors) to a separate recoverable/dead-letter mechanism (similar to `pallet-message-queue`'s `OverweightEnqueued`/`execute_overweight` design) instead of rolling back the nonce together with the failure. This ensures one bad/unroutable message cannot block the sequence for legitimate subsequent messages.

### Proof of Concept
1. Relayer submits a valid, verifiable Ethereum event proof for nonce `N` on channel `C` whose payload is `Command::SendNativeToken` with a `token_id` that cannot be reanchored to the destination's universal location (triggers `ConvertMessageError::CannotReanchor` deterministically), or whose resulting XCM exceeds `T::MaxMessageSize`/router hard limit (triggers `SendError::ExceedsMaxMessageSize`).
2. `submit()` verifies proof successfully, bumps `Nonce[C]` to `N` inside the storage transaction, then fails at `do_convert`/`send_xcm`, returning `Err`; the whole dispatch — including the nonce bump — is rolled back (`Nonce[C]` remains `N-1`).
3. Any later legitimate message with nonce `N` (or higher) can never be accepted because `submit()` requires `envelope.nonce == nonce.saturating_add(1)`, and resubmitting the poison message `N` fails identically every time.
4. Channel `C` is permanently stalled; only root can `set_operating_mode(Halted)`, which stops the entire pallet rather than resolving the specific channel.

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

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L337-341)
```rust
		pub fn send_xcm(xcm: Xcm<()>, dest: ParaId) -> Result<XcmHash, Error<T>> {
			let dest = Location::new(1, [Parachain(dest.into())]);
			let (xcm_hash, _) = send_xcm::<T::XcmSender>(dest, xcm).map_err(Error::<T>::from)?;
			Ok(xcm_hash)
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L428-437)
```rust
		let total_fee_asset: Asset = (Location::parent(), asset_hub_fee).into();

		let asset_loc =
			ConvertAssetId::maybe_convert(token_id).ok_or(ConvertMessageError::InvalidToken)?;

		let mut reanchored_asset_loc = asset_loc.clone();
		reanchored_asset_loc
			.reanchor(&GlobalAssetHubLocation::get(), &EthereumUniversalLocation::get())
			.map_err(|_| ConvertMessageError::CannotReanchor)?;

```

**File:** substrate/frame/message-queue/src/lib.rs (L127-138)
```rust
//! # Scenario: Overweight execution
//!
//! A permanently over-weight message which was skipped by the message processing will never be
//! executed automatically through `on_initialize` nor by calling
//! [`frame_support::traits::ServiceQueues::service_queues`].
//!
//! Manual intervention in the form of
//! [`frame_support::traits::ServiceQueues::execute_overweight`] is necessary. Overweight messages
//! emit an [`Event::OverweightEnqueued`] event which can be used to extract the arguments for
//! manual execution. This only works on permanently overweight messages. There is no guarantee that
//! this will work since the message could be part of a stale page and be reaped before execution
//! commences.
```
