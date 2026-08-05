The claim accurately reflects the code in this repository. The `add_tip` implementation only checks `amount > 0` and that the nonce is not already consumed via `Nonce::<T>::get`, with no validation that the nonce corresponds to any known, submitted, or pending message. [1](#0-0) 

The `Tips` storage map has no eviction/refund path — the only place it is read is `Tips::<T>::take(nonce)` inside `process_message`, which only fires for a nonce that is actually submitted and successfully processed. [2](#0-1) 

Since nonces are tracked only via a `NonceBitmap`/`SparseBitmapImpl` marking "processed" nonces [3](#0-2) , there is no registry of "pending" or "valid future" nonces to check against, so any arbitrary `u64` nonce (including ones that will never be submitted) passes the `NonceConsumed` check and the tip is accepted and stored permanently. This contrasts with the outbound-queue-v2 pallet, which validates against `PendingOrders` and returns `AddTipError::UnknownMessage` for unknown nonces — a check inbound-queue-v2 lacks entirely, despite `AddTipError` defining an unused `UnknownMessage` variant. [4](#0-3) 

This matches the exploit path described: a caller can invoke `add_tip` with an arbitrary/future nonce that will never be processed, permanently locking the tipped funds in `Tips` storage with no extrinsic to reclaim them. The invariant violated — "bridge rewards must conserve value and settle exactly once to the rightful beneficiary" and "payout state must only advance after ... settlement succeed atomically" — is directly applicable since funds are locked with no possible recovery.

Audit Report

## Title
Inbound queue v2 `add_tip` accepts tips for non-existent/future nonces, permanently locking value in `Tips` storage - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

## Summary
`AddTip::add_tip` in the inbound-queue-v2 pallet only checks that `amount > 0` and that the nonce has not already been processed (via `Nonce::<T>::get`), but never verifies that the nonce corresponds to a message that has been or will be submitted. Any caller can tip an arbitrary/future nonce, and if that nonce is never submitted, the tip is permanently stranded in `Tips::<T>` with no extrinsic capable of reclaiming it.

## Finding Description
`add_tip` (bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs, lines 248-259) performs only two checks: `amount > 0` and `!Nonce::<T>::get(nonce)`. The `Nonce` bitmap is only set when `process_message` (lines 215-245) successfully processes a message for that nonce; there is no tracking of "pending"/"known-submitted" nonces analogous to outbound-queue-v2's `PendingOrders`. Consequently, `add_tip` accepts tips keyed on any `u64` value that has simply never been consumed — including nonces far beyond the current sequence that will never be submitted. The tip is written unconditionally via `Tips::<T>::mutate`. The only code path that ever reads and removes a `Tips` entry is inside `process_message`, gated on that specific nonce actually being submitted and successfully processed. If that never happens, the entry remains in storage indefinitely with no refund or reclaim mechanism, unlike the outbound-queue-v2 pallet which validates against `PendingOrders` and returns `AddTipError::UnknownMessage` for unrecognized nonces (bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs, lines 483-496). The `AddTipError` enum even defines an `UnknownMessage` variant that inbound-queue-v2 never uses.

## Impact Explanation
This is a permanent user-fund lock: value deposited via `add_tip` for a nonce that is never submitted/processed can never be paid out or reclaimed, violating the requirement that bridge reward/payout state settle exactly once to the rightful beneficiary. The `add_tip` capability is intended to be a public/permissionless incentivization mechanism (exposed indirectly through system-v2/system-frontend wrappers), so this is reachable by any account with access to that call, without requiring privileged access, a compromised relayer, or off-chain infrastructure control.

## Likelihood Explanation
High. No special conditions are required: the caller only needs to supply a nonce that has not yet been consumed, which trivially includes any nonce not yet reached by the sequential nonce counter (e.g., `u64::MAX`). There is no on-chain registry of legitimate pending nonces to check against, so this can be triggered by user error (typo) or deliberately by any party with access to the `add_tip` path.

## Recommendation
Mirror the outbound-queue-v2 design: track pending/known nonces (e.g., nonces that have been observed as forthcoming, or restrict tipping to nonces at or below some validated bound) and reject `add_tip` calls for nonces not associated with a legitimately expected message, returning `AddTipError::UnknownMessage`. Alternatively, implement a timeout/refund mechanism allowing the original tipper to reclaim tips for nonces that remain unprocessed after a defined window.

## Proof of Concept
1. Call `InboundQueue::add_tip(nonce = u64::MAX, amount = 1_000_000)` (via whatever public wrapper exposes this trait call).
2. `Nonce::<T>::get(u64::MAX)` returns `false` (never processed), so `NonceConsumed` check passes.
3. `Tips::<T>::mutate(u64::MAX, ...)` stores `1_000_000`.
4. No message with nonce `u64::MAX` will ever be submitted/processed (nonces are sequential and far lower), so `Tips::<T>::take(u64::MAX)` in `process_message` is never invoked.
5. The `1_000_000` tip remains in `Tips` storage permanently; no extrinsic exists to remove or refund it.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L165-168)
```rust
	/// StorageMap used for encoding a SparseBitmapImpl that tracks whether a specific nonce has
	/// been processed or not. Message nonces are unique and never repeated.
	#[pallet::storage]
	pub type NonceBitmap<T: Config> = StorageMap<_, Twox64Concat, u64, u128, ValueQuery>;
```

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
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
	}
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L32-37)
```rust
#[derive(Debug, Encode, PartialEq, DecodeWithMemTracking, Decode, TypeInfo, PalletError)]
pub enum AddTipError {
	NonceConsumed,
	UnknownMessage,
	AmountZero,
}
```
