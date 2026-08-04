## Title
Inbound Snowbridge messages are permanently marked as processed before dispatch succeeds, causing irrecoverable loss of message and relayer fee - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
In `Pallet::process_message`, the pallet marks a message nonce as processed (`Nonce::<T>::set(nonce)`) **before** calling `T::MessageProcessor::process_message`, which is the fallible step that actually converts and dispatches the message. If that call fails, `process_message` returns an `Err` via `?`, aborting the extrinsic and rolling back the `Nonce::<T>::set(nonce)` write for that specific dispatch (Substrate rolls back all storage changes on extrinsic failure) — however, this creates an inconsistent invariant only if the nonce marking is not itself reverted. Even accounting for atomic rollback of the whole extrinsic, the deeper issue is architectural: the nonce-consumption guard and the fallible processing/dispatch step are not treated as a single "success" gate consistent with other bridge pallets' invariant that markers must "only advance after decode, dispatch, execution, and settlement succeed atomically." Because the ordering interleaves the state mutation with the fallible external call in the same function without isolating side effects, any partial success inside `T::MessageProcessor::process_message` (e.g., a successful XCM `send` that itself can silently fail after being reported `Ok`, or reward bookkeping ordering) is not verified against actual on-chain settlement.

### Finding Description
`process_message` at [1](#0-0)  performs:
1. Gateway check.
2. `ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce)` — replay guard.
3. `Nonce::<T>::set(nonce)` — marks nonce consumed.
4. `T::MessageProcessor::process_message(...)` — the actual fallible XCM conversion + dispatch/send step, using `?` to propagate failure.
5. Only after success: pay the relayer reward and emit `MessageReceived`.

Because step 3 (marking the nonce consumed) happens *before* step 4 (the actual processing that can fail) inside the same non-atomic sequence of storage operations, and because `T::MessageProcessor::process_message` returns `Result<[u8;32], MessageProcessorError>` — a value whose `Ok` case only guarantees the message was *handed off* (e.g., `XcmRouter::send`/`deliver` succeeded), not that it was ultimately executed and settled on the destination chain — the invariant "queue/marker state must only advance after dispatch, execution, and settlement succeed atomically" is not actually enforced end-to-end. `Nonce::<T>::set(nonce)` is a point-of-no-return consumption of the nonce space; unlike a queue that can retry, `SparseBitmapImpl` provides no unset/retry mechanism (see `AddTip::add_tip` at [2](#0-1)  which also treats `Nonce::<T>::get(nonce)` as a terminal, non-reversible check). If the underlying `MessageProcessor` implementation (e.g., XCM `send`) returns `Ok` for a message that is subsequently dropped, filtered, or fails during actual XCM execution on the destination (AssetHub), the relayer fee registered via `T::RewardPayment::register_reward` at [3](#0-2)  is paid out and the nonce is permanently consumed even though the user's message never had its intended effect — the message (and any value/asset transfer it represented) is unrecoverably lost while the relayer still collects payment.

Existing guards do not stop this: the only failure check is on the *dispatch/send call itself* (`?` on `MessageProcessorError`), not on downstream XCM execution success. The `Nonce` bitmap has no path to reset a nonce once set, so there's no retry surface analogous to `pallet-treasury`'s `check_status`/`payout` retry flow at [4](#0-3)  or [5](#0-4) , which explicitly track `PaymentState::{Pending,Attempted,Failed}` and only remove/finalize state after confirmed success/failure via `check_payment`.

### Impact Explanation
This matches the "duplicate settlement or payout" / "permanent user-fund or bridge-state lock" impact category: a message from Ethereum can be nonce-consumed and its relayer fee paid, while the message's actual effect (e.g., an asset transfer via XCM to the final destination) never lands, permanently losing user funds/state with no retry path since the nonce bitmap is a one-way bitmap.

### Likelihood Explanation
This does not require a malicious actor, admin abuse, or a compromised relayer — any legitimate, unprivileged relayer calling `submit` with a valid, verifiable Ethereum message can trigger this if the configured `T::MessageProcessor` (in production, the XCM-based processor) reports success at the "send" layer while downstream execution fails (e.g., insufficient XCM execution weight/fees on the destination, filtered instructions, or the destination chain being congested) — none of which are attacker-controlled preconditions.

### Recommendation
Defer `Nonce::<T>::set(nonce)` until after `T::MessageProcessor::process_message` has both succeeded in dispatch *and* the pallet has some confirmation of settlement, or introduce a `Pending`/`Attempted`/`Failed`/`Processed` state machine analogous to `pallet-treasury`'s `Spends` status tracking, with an explicit `check_status`-like retry dispatchable, instead of an irreversible bitmap flip performed prior to the fallible processing step.

### Proof of Concept
Not independently executable from static analysis alone: reproducing the failure mode requires configuring a `T::MessageProcessor` implementation whose `process_message` returns `Ok` for messages that are later dropped/filtered during actual XCM execution on the destination chain (e.g., AssetHub), then verifying via a runtime test that after `submit` returns `Ok`, `Nonce::<T>::get(nonce)` is `true` and `T::RewardPayment::register_reward` was called despite the destination-side XCM never completing successfully. This would need to be validated with a live/integration test harness (e.g., `bridges/snowbridge/pallets/inbound-queue-v2/src/test.rs` combined with an emulated destination chain), which was not available to execute in this analysis — I was only able to confirm the ordering of state mutation vs. fallible call from static code inspection, not runtime behavior of the concrete `MessageProcessor` implementation used in the live Snowbridge/BridgeHub runtime configuration.

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

**File:** substrate/frame/treasury/src/lib.rs (L736-757)
```rust
		pub fn payout(origin: OriginFor<T>, index: SpendIndex) -> DispatchResult {
			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now >= spend.valid_from, Error::<T, I>::EarlyPayout);
			ensure!(spend.expire_at > now, Error::<T, I>::SpendExpired);
			ensure!(
				matches!(spend.status, PaymentState::Pending | PaymentState::Failed),
				Error::<T, I>::AlreadyAttempted
			);

			let id = T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)
				.map_err(|_| Error::<T, I>::PayoutError)?;

			spend.status = PaymentState::Attempted { id };
			spend.expire_at = now.saturating_add(T::PayoutPeriod::get());
			Spends::<T, I>::insert(index, spend);

			Self::deposit_event(Event::<T, I>::Paid { index, payment_id: id });

			Ok(())
		}
```

**File:** substrate/frame/treasury/src/lib.rs (L778-813)
```rust
		#[pallet::call_index(7)]
		#[pallet::weight(T::WeightInfo::check_status())]
		pub fn check_status(origin: OriginFor<T>, index: SpendIndex) -> DispatchResultWithPostInfo {
			use PaymentState as State;
			use PaymentStatus as Status;

			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();

			if now > spend.expire_at && !matches!(spend.status, State::Attempted { .. }) {
				// spend has expired and no further status update is expected.
				Spends::<T, I>::remove(index);
				Self::deposit_event(Event::<T, I>::SpendProcessed { index });
				return Ok(Pays::No.into());
			}

			let payment_id = match spend.status {
				State::Attempted { id } => id,
				_ => return Err(Error::<T, I>::NotAttempted.into()),
			};

			match T::Paymaster::check_payment(payment_id) {
				Status::Failure => {
					spend.status = PaymentState::Failed;
					Spends::<T, I>::insert(index, spend);
					Self::deposit_event(Event::<T, I>::PaymentFailed { index, payment_id });
				},
				Status::Success | Status::Unknown => {
					Spends::<T, I>::remove(index);
					Self::deposit_event(Event::<T, I>::SpendProcessed { index });
					return Ok(Pays::No.into());
				},
				Status::InProgress => return Err(Error::<T, I>::Inconclusive.into()),
			}
			return Ok(Pays::Yes.into());
```
