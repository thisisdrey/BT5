### Title
Inbound Snowbridge V2 messages latch `Nonce` and pay relayer reward before XCM execution on AssetHub is confirmed, permanently losing bridged funds on downstream failure - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_message` in the Snowbridge Inbound Queue V2 pallet marks a bridged message's nonce as consumed and pays out the relayer's reward as soon as the XCM message has been *sent* (queued) to AssetHub, not once it has actually been executed there. Because the nonce bitmap can only ever transition from unset to set and has no unset/retry path, any downstream failure of the XCM on AssetHub (barrier rejection, insufficient weight, asset conversion failure, etc.) permanently strands the bridged asset while the relayer is already paid, with no way to resubmit. This mirrors the Holograph `executeJob` bug: state that finalizes "delivery" is committed before the actual value transfer is verified to have succeeded, and once committed there is no recovery path.

### Finding Description
`process_message` performs, in this order:
1. `ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce)` – reject replays.
2. `Nonce::<T>::set(nonce)` – latch the nonce as consumed. [1](#0-0) 
3. `T::MessageProcessor::process_message(...)` which converts the message to XCM and calls `Sender::deliver(ticket)` to enqueue it for asynchronous delivery to AssetHub via XCMP/DMP. [2](#0-1) [3](#0-2) 
4. Pay the relayer `relayer_fee + tip` via `T::RewardPayment::register_reward`, and emit `MessageReceived`. [4](#0-3) 

`Sender::deliver` only guarantees the XCM was successfully *queued* for cross-consensus transport; it says nothing about whether the message will be *executed* successfully on AssetHub. XCM execution failures on the destination (e.g. `Barrier` denial, insufficient weight for the full instruction set, asset id conversion mismatch, `FailedToTransactAsset`) occur asynchronously in a different runtime/block and cannot roll back the BridgeHub-side `submit` extrinsic that already committed via FRAME's atomic storage transaction. Since `Nonce::<T>::set(nonce)` is a monotonic, one-way bitmap with no reset call anywhere in the pallet, the nonce can never be resubmitted once consumed — there is no retry mechanism analogous to the `_failedJobs` flag replay path the Holograph report discusses. The relayer is paid `relayer_fee + tip` for merely queuing the message, regardless of whether the beneficiary's assets actually arrive at AssetHub. `AddTip::add_tip` likewise treats "nonce set" as final ("If the nonce is already processed, return an error"), reinforcing that the protocol treats send-success as final settlement. [5](#0-4) 

This is the direct structural analog of the Holograph bug: `delete _operatorJobs[hash]` happens before the outcome of the actual value-moving call is known, and a failure in that call (caught, not reverted) leaves the job permanently non-replayable while the underlying asset is lost. Here, `Nonce::<T>::set(nonce)` and reward payment happen before the actual cross-chain execution outcome is known, and a failure on the destination chain (which cannot revert the source-chain state) leaves the nonce permanently consumed with no replay path.

### Impact Explanation
If the XCM constructed from an inbound Ethereum message fails to execute on AssetHub for any legitimate, non-malicious reason (fee/weight misestimation, asset registration edge case, barrier/filter changes, `ConvertMessage`/`AccountToLocation` mismatches for exotic asset combinations), the underlying bridged value (Ether or tokens locked on Ethereum, intended to be minted/unlocked on AssetHub) is unrecoverable through the inbound queue: the nonce is burned so `submit` can never be replayed for that message, and the relayer has already collected `relayer_fee + tip` for a delivery that never actually completed for the beneficiary. This is a permanent user-fund lock combined with unbacked relayer payout for undelivered value — squarely within the required impact categories (permanent user-fund/bridge-state lock, duplicate/mis-targeted settlement).

### Likelihood Explanation
This does not require a malicious relayer, validator, or governance actor — it only requires an ordinary XCM execution failure on the destination chain, which is a normal operational occurrence in cross-consensus messaging (weight misestimation, barrier changes, asset registration drift are common causes). Any relayer submitting a message whose XCM happens to fail downstream triggers this path unintentionally, making the likelihood non-trivial for a live bridge under real network conditions, especially during runtime upgrades on AssetHub that change execution semantics.

### Recommendation
Do not treat `Sender::deliver` success as final settlement. Options:
- Defer `Nonce::<T>::set` and reward payment until execution confirmation is available (e.g., via a receipt/ack mechanism from AssetHub, or by using `ExecuteXcm` locally with weight metering strong enough to guarantee completion rather than fire-and-forget `SendXcm`).
- If asynchronous delivery must be used, add an explicit governance/permissionless recovery path keyed by nonce that allows re-issuing the downstream XCM (not the whole inbound message) if execution provably failed, similar to XCM's own `AssetTrap`/`claim_assets` mechanism, and ensure relayer reward is contingent on confirmed execution, not mere enqueueing.
- At minimum, document and bound this as an accepted async-messaging risk with an explicit recovery flow (comparable to Arbitrum retryable tickets, as referenced in the original report's judge comment) rather than leaving the nonce bitmap monotonic with no unset path.

### Proof of Concept
1. A relayer submits a valid, verifiable Ethereum event via `InboundQueueV2::submit` for nonce `N` carrying an asset transfer intended for AssetHub.
2. `process_message` executes: `Nonce::<T>::set(N)` commits; `Converter::convert` succeeds; `Sender::deliver` succeeds in queuing the XCM (e.g., into UMP/XCMP outbound queue) — `submit` returns `Ok`, the relayer is paid `relayer_fee + tip`, and `MessageReceived { nonce: N, .. }` is emitted. [6](#0-5) 
3. On AssetHub, the delivered XCM later fails to execute fully (e.g., insufficient weight limit configured for the instruction set, or a `Barrier`/filter rejecting part of the program) — this failure is entirely on the destination chain and cannot revert BridgeHub's already-committed state from step 2.
4. The relayer attempts no further action (they were already paid); the beneficiary's intended asset never lands on AssetHub.
5. Any retry attempt via `InboundQueueV2::submit` with the same event/nonce `N` now fails with `Error::<T>::InvalidNonce` at `ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce)`, and there is no other call in the pallet to unset `NonceBitmap` for `N`. [7](#0-6) 
6. Result: funds intended for the beneficiary are permanently unrecoverable through this pallet, while the relayer already received `relayer_fee + tip` for the failed delivery — an exact structural match to the Holograph `executeJob` finding (state finalized as "delivered" before the value-moving step's real outcome is confirmed, with no replay path once finalized).

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L215-245)
```rust
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
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
