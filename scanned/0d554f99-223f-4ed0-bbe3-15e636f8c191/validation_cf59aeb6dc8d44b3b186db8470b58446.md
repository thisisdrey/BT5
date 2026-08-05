### Title
Inbound queue v2 `add_tip` accepts tips for non-existent / future nonces, allowing unprivileged users to lock value in `Tips` storage that can never be paid out - (`File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
The external report's core invariant is: a query function for an invalid identifier should fail/revert, but instead silently returns a default value (empty string). In the Snowbridge Inbound Queue V2 pallet, `AddTip::add_tip` for an invalid/future `nonce` does not revert; it silently stores the tip in `Tips::<T>` even though no corresponding message will ever be processed for that nonce. This is the same broken invariant: an operation against a non-existent identifier is accepted instead of rejected, leaving value stranded.

### Finding Description
In `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`, `add_tip` only checks:
1. `amount > 0`
2. `!Nonce::<T>::get(nonce)` (i.e. the nonce has not already been processed)

It does **not** verify that the `nonce` corresponds to a message that has been submitted or is pending. Because nonces are sequential and `Nonce::<T>` is only set when `process_message` succeeds, any future nonce (or any nonce that will never be submitted) is currently "not processed", so `add_tip` succeeds and writes to `Tips::<T>`.

When `process_message` is later called for that nonce, the tip is taken and paid to the relayer. But if the nonce is never submitted (e.g. user typo, front-end bug, or attacker deliberately tips nonce `u64::MAX`), the tip sits in `Tips` storage forever. There is no extrinsic to refund or reclaim `Tips` balances.

### Impact Explanation
- **Permanent user-fund lock**: an unprivileged caller (any signed origin via the frontend/system-v2 `add_tip` wrapper, or any caller able to invoke `AddTip`) can deposit a tip for a nonce that will never be processed. The funds are added to `Tips` and never returned.
- This violates the "bridge rewards ... must conserve value and settle exactly once to the rightful beneficiary" pivot and the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot.
- It is a public, unprivileged path: `add_tip` has no origin check inside the inbound-queue pallet; the origin check is delegated to `system-v2`/`system-frontend`, but the underlying `InboundQueue::add_tip` trait implementation itself accepts any nonce that is merely "not yet processed".

### Likelihood Explanation
High. The `add_tip` path is intentionally public (relayer incentivization). A user or frontend only needs to supply a `nonce` that has not yet been processed. Because nonces are not pre-registered, there is no on-chain list of "valid pending nonces" to check against. Any `u64` value that has not been consumed is accepted. The existing integration test `tip_to_invalid_nonce_is_added_to_lost_tips` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs` demonstrates that tipping an invalid nonce is an expected failure mode for the *outbound* path, but the inbound path lacks an equivalent "unknown message" check and instead silently stores the tip.

### Recommendation
Modify `InboundQueue::add_tip` to reject tips for nonces that do not correspond to a message already known to the pallet. Options:
1. Require the message to have been submitted first and track pending nonces in a separate storage map; only allow `add_tip` for nonces in that pending set.
2. If tips must be addable before submission, implement a timeout/refund mechanism so that tips for nonces that are never processed within a window can be reclaimed by the original tipper.
3. At minimum, mirror the outbound-queue behavior and return `AddTipError::UnknownMessage` when the nonce is not associated with a known pending message.

### Proof of Concept
Consider the existing code in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`:

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

An attacker calls `add_tip(nonce = u64::MAX, amount = 1_000_000)`. `Nonce::<T>::get(u64::MAX)` is `false`, so the call succeeds and `Tips::<T>::insert(u64::MAX, 1_000_000)` is stored. No inbound message with nonce `u64::MAX` will ever be processed (nonces are sequential and far lower), so the tip is permanently locked. There is no extrinsic to withdraw it.

### Supporting Citations
- `add_tip` implementation with only `NonceConsumed` and `AmountZero` checks: [1](#0-0) 
- `Tips` storage definition: [2](#0-1) 
- `process_message` takes the tip with `unwrap_or_default` and pays it: [3](#0-2) 
- `AddTipError` enum lacks an `UnknownMessage` variant for inbound: [4](#0-3) 
- Outbound queue `add_tip` correctly returns `UnknownMessage` for missing pending orders: [5](#0-4)

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L174-178)
```rust
	/// Keep track of tips added for a message as an additional relayer incentivization. The
	/// key for the storage map is the nonce of the message to which the tip should be added.
	/// The value is the tip amount, in Ether.
	#[pallet::storage]
	pub type Tips<T: Config> = StorageMap<_, Blake2_128Concat, u64, u128, OptionQuery>;
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L234-239)
```rust
			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```
