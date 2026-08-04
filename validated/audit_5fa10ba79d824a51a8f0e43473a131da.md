## Analysis

The external report's core broken invariant: **when a batched cross-chain call execution fails, the executor contract reverts entirely without persisting any "attempted" marker, so the same failing payload can be replayed indefinitely** — draining relayer gas and making it impossible to distinguish "never executed" from "executed-but-failed".

The direct structural analog exists in the Snowbridge `snowbridge-pallet-inbound-queue-v2` pallet's `process_message` function.### Title
Failed inbound XCM processing leaves relayed Ethereum message nonce unmarked, enabling unbounded replay and non-idempotent re-execution - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
`snowbridge-pallet-inbound-queue-v2`'s `process_message` marks a message nonce as "received" via `Nonce::<T>::set(nonce)` *before* invoking `T::MessageProcessor::process_message`. If the processor call fails (XCM send failure, insufficient fees, unreachable destination, asset conversion error, etc.), the dispatchable returns `Err`, and FRAME's standard transactional dispatch semantics roll back **all** storage writes made during that call — including the nonce bit that was just set. This is the exact bug class from the `CrossChainExecutor` report: a failing message leaves no trace that execution was attempted, so it can be resubmitted forever, and the pallet cannot distinguish "never processed" from "processed but failed."

### Finding Description [1](#0-0) 

```rust
pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
    ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);
    let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);
    ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

    // Mark message as received
    Nonce::<T>::set(nonce);

    let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
        .map_err(|e| match e { ... })?;   // <-- propagates Err(...)
    ...
    Ok(())
}
```

`Nonce::<T>::set(nonce)` at line 225 happens **before** `T::MessageProcessor::process_message` at line 227. The processor (`XcmMessageProcessor`, see `bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs`) can legitimately fail for reasons that are entirely independent of message validity:
- `SendError::Fees` if `Executor::charge_fees` fails (fee payer account underfunded),
- `SendError::NotApplicable` / `SendError::DestinationUnsupported` if the XCM router cannot currently route,
- `ConvertMessageError` variants if the asset/location state changed.

When any of these occur, `process_message` returns `Err(...)`. Because this is a `#[pallet::call]` dispatchable (invoked via `submit`), FRAME guarantees that **any storage mutation performed during a failed dispatchable is rolled back** — this is the same guarantee exercised and asserted elsewhere in the repository, e.g. the `pallet-revive`/`pallet-assets` precompile test explicitly documents and verifies this behavior: [2](#0-1) 

That test shows that when a later step in a dispatchable fails, *every* prior storage write in the same call — including a nonce increment — is rolled back atomically. The same mechanism applies to `InboundQueueV2::submit` → `process_message`: the `Nonce::<T>::set(nonce)` write is undone whenever `T::MessageProcessor::process_message` errors, leaving `Nonce::<T>::get(nonce)` at `false` after the failed submission.

Compare this with the sibling `snowbridge-pallet-inbound-queue` (v1) pallet, which increments/persists the channel nonce via `<Nonce<T>>::try_mutate` and *then* still performs further fallible operations (`burn_fees`, `send_xcm`) that can fail and cause the same nonce write to be rolled back — the same class of issue exists there too.

### Impact Explanation
- **Unbounded relayer griefing / DoS on relayer economics**: An attacker can craft an Ethereum-side event (e.g., pointing to a destination/asset that reliably fails `PayFees`/`SendXcm` on BridgeHub, or referencing an asset not yet registered) that will always fail `MessageProcessor::process_message`. Every relayer who observes the corresponding Ethereum event and calls `submit` pays the full `submit()` extrinsic weight/fee, but the nonce is never marked, so the message remains eligible for resubmission by any relayer indefinitely (nonce never advances to a "processed" state).
- **Ambiguous / non-idempotent state**: Since the same message can be relayed again at any point in the future without expiry, if the on-chain conditions that caused the earlier failure change later (e.g., the sovereign/fee-payer account is later funded, or the destination becomes routable again), the identical message can suddenly succeed at an arbitrary future block — producing delayed, out-of-order execution of a "stale" bridge message that the emitting side (or governance) may have assumed was dead. This mirrors exactly the Medium-severity concern raised by the original judge: lack of expiry on failed cross-chain calls can create non-idempotent behavior with correctness implications for the receiving system.
- This does not require a malicious validator, relayer, or governance actor — any permissionless caller of the public `submit` extrinsic can trigger and repeat this behavior using an event that was legitimately proven via the light-client `Verifier` (the proof itself is valid; only the downstream processing fails).

### Likelihood Explanation
High. `submit` is a public, permissionless extrinsic (`ensure_signed` only) that any relayer can call for any correctly-proven Ethereum event. Causing `MessageProcessor::process_message` to fail only requires constructing a message whose XCM fee payment or destination routing fails at execution time (e.g., referencing an asset/location not yet registered on AssetHub, or a fee payer account with insufficient balance) — none of which require privileged access. There is no cooldown, backoff, or "attempted-and-failed" marker to prevent immediate and repeated resubmission of the exact same nonce.

### Recommendation
Persist an explicit "attempted" state for the nonce (e.g., a tri-state: unseen / processed / failed) independent of the transactional rollback of the dispatchable, or move the `Nonce::<T>::set(nonce)` write to occur in a way that survives regardless of the outcome of `MessageProcessor::process_message` (e.g., by wrapping only the processor call in its own inner transactional scope with `storage::with_transaction`, catching the error, and then unconditionally committing the nonce/failure marker in the outer scope before returning `Ok(())` with a `MessageProcessingFailed` event, rather than propagating the error and rolling back the whole extrinsic). Additionally, consider adding an expiry/TTL for messages so that a message that failed once cannot be silently re-attempted an arbitrary time later under different chain conditions.

### Proof of Concept
1. On Ethereum, emit a valid Gateway event for `nonce = N` whose payload, once converted to XCM via `MessageToXcm`, requires paying execution fees that the relayer's converted `fee_payer` location cannot cover (or targets a destination the `XcmRouter` currently cannot reach).
2. A relayer submits `EventProof` via `InboundQueueV2::submit(origin, event)`. `T::Verifier::verify` succeeds (proof is valid), `Message::try_from` succeeds, `process_message` sets `Nonce::<T>::set(N)`, then calls `T::MessageProcessor::process_message`, which fails with `SendError::Fees` (or `NotApplicable`). `process_message` returns `Err(Error::<T>::FeesNotMet)` (or `Unreachable`).
3. Because the extrinsic returned `Err`, the entire storage transaction for `submit` rolls back — `Nonce::<T>::get(N)` is `false` again (verified analogous rollback semantics for a nonce write on dispatch failure in `substrate/frame/assets/precompiles/src/permit_precompile_tests.rs::permit_rollback_does_not_increment_nonce`).
4. The relayer (or any other relayer, or the attacker themselves) can call `submit` again with the identical `EventProof` for nonce `N`. `ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce)` passes again since the nonce bit was never persisted, and the same failure (and gas cost) repeats — indefinitely, with no cost to the attacker beyond the extrinsic's base weight, and full cost to whichever relayer keeps attempting it.

### Citations

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

**File:** substrate/frame/assets/precompiles/src/permit_precompile_tests.rs (L593-625)
```rust
#[test]
fn permit_rollback_does_not_increment_nonce() {
	use frame_support::traits::fungibles::approvals::Inspect;

	new_test_ext().execute_with(|| {
		let setup = permit_setup(PRECOMPILE_ADDRESS_PREFIX);

		let (v, r, s) =
			sign_permit(setup.asset_addr, setup.spender_addr, AlloyU256::from(100), setup.deadline);

		assert_ok!(Assets::freeze_asset(
			RuntimeOrigin::signed(setup.owner_account),
			setup.asset_id
		));

		let result = raw_permit(
			setup.submitter,
			setup.asset_addr,
			HARDHAT_ACCOUNT_0,
			setup.spender_addr,
			AlloyU256::from(100),
			setup.deadline,
			v,
			r,
			s,
		);
		assert_permit_dispatch_err(result, pallet_assets::Error::<Test>::AssetNotLive);

		assert_eq!(
			permit::Pallet::<Test>::nonce(&setup.asset_addr, &HARDHAT_ACCOUNT_0),
			U256::zero(),
			"nonce must remain 0 when the storage transaction rolls back"
		);
```
