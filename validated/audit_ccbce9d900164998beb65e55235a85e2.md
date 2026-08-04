### Title
`add_tip` in the Snowbridge System-V2 pallet always returns `Ok(())` and permanently strands user funds in `LostTips` when the tip target cannot be found - ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

### Summary
The external report's core broken invariant is: a for-loop/lookup that fails to find its target still lets the caller believe the operation completed (an event is emitted as if the removal succeeded), and the call is not failed. The same "search miss, still return success, but silently accept it" pattern exists in `EthereumSystemV2::add_tip`, which never fails the extrinsic (`-> DispatchResult` always returns `Ok(())`) even when the underlying nonce/order lookup used to attach the tip cannot find its target, and the tip amount is shoveled into a `LostTips` map that this repository has no extrinsic to drain.

### Finding Description
`add_tip` in [1](#0-0)  dispatches to `InboundQueue::add_tip` or `OutboundQueue::add_tip` depending on `message_id`, both of which perform a storage lookup for the given nonce (`Nonce::<T>::get` / `Tips::<T>::mutate` in the inbound queue, and `PendingOrders::<T>::try_mutate_exists` in the outbound queue) and return `AddTipError::NonceConsumed` or `AddTipError::UnknownMessage` when the nonce/order is not found: [2](#0-1) [3](#0-2) 

When that lookup fails, `EthereumSystemV2::add_tip` does not propagate the error to the caller. Instead it swallows it, records the amount into `LostTips` for later "recovery" and unconditionally returns `Ok(())`: [4](#0-3) 

The storage doc comment itself admits there is currently no recovery path: [5](#0-4) 

This is the same class of bug as the ExperiPie report: the code searches for an item (a nonce/order), the search can miss, and instead of failing the call/transaction it continues down a path that leaves the caller/state in a false or dead-end condition — here, the amount debited from the sender (via the frontend pallet flow that proxies this call across chains) is moved into `LostTips` with no extrinsic anywhere in the repository to reclaim it, making the loss permanent.

### Impact Explanation
Since `add_tip` is reached via `T::FrontendOrigin` (an XCM-origin check, not a privileged/root check), any unprivileged user on the origin chain who pays a tip for a message whose nonce has already been consumed (a natural, non-malicious race: the message gets processed before or concurrently with the tip XCM landing on BridgeHub) permanently loses the tip amount. `LostTips` only accumulates; there is no path in this codebase to withdraw or refund it. This matches the "permanent user-fund or bridge-state lock" impact category.

### Likelihood Explanation
This is a normal race condition, not an attack requiring a malicious relayer/validator: any legitimate user tipping a nearly-finalized message can lose funds simply due to network timing between message processing and tip delivery across chains. The pallet's own tests (`add_tip_inbound_fails_when_nonce_is_consumed`) demonstrate and assert this exact "success: false, but call still returns Ok and money is stuck in LostTips" behavior as intended, confirming the condition is reachable and expected to occur in production, not just a theoretical edge case: [6](#0-5) 

### Recommendation
Either (a) provide a concrete extrinsic/mechanism to reclaim `LostTips` balances back to the original sender, or (b) fail the whole cross-chain tip flow atomically (e.g. only debit the sender on the origin chain after confirmation the tip was actually attached) instead of unconditionally returning `Ok(())` and stranding the funds — mirroring the report's recommendation to "fail the call or not emit the misleading success/removal event."

### Proof of Concept
1. A relayer message with nonce `N` is submitted and processed via `InboundQueue::process_message` (setting `Nonce::<T>::set(N)`), or the corresponding `PendingOrders` entry for outbound nonce `N` is finalized/removed, concurrently with a tip request in flight.
2. A user's `add_tip` request for `MessageId::Inbound(N)` (or `Outbound(N)`) arrives at `EthereumSystemV2::add_tip` after this point.
3. `InboundQueue::add_tip`/`OutboundQueue::add_tip` returns `Err(AddTipError::NonceConsumed | UnknownMessage)`.
4. `EthereumSystemV2::add_tip` catches the error, adds `amount` to `LostTips::<T>::get(sender)`, emits `TipProcessed { success: false, .. }`, and returns `Ok(())` regardless — see [7](#0-6) .
5. There is no extrinsic in the repository that reads and clears `LostTips` back to the user; the funds recorded there are permanently unspendable/unrecoverable, exactly as validated by the existing test asserting `lost_tip == 1000` with no further pallet call able to reclaim it.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-142)
```rust
	/// Relayer reward tips that were paid by the user to incentivize the processing of their
	/// message, but then could not be added to their message reward (e.g. the nonce was already
	/// processed or their order could not be found). Capturing the lost tips here supports
	/// implementing a recovery method in the future.
	#[pallet::storage]
	pub type LostTips<T: Config> =
		StorageMap<_, Blake2_128Concat, AccountIdOf<T>, u128, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
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

**File:** bridges/snowbridge/pallets/system-v2/src/tests.rs (L197-219)
```rust
#[test]
fn add_tip_inbound_fails_when_nonce_is_consumed() {
	new_test_ext(true).execute_with(|| {
		let origin = make_xcm_origin(FrontendLocation::get());
		let sender: AccountId = Keyring::Alice.into();
		// In `MockOkInboundQueue`, the mocked implementation returns an error when the nonce is
		// equal to 3, to simulate an error condition.
		let message_id = MessageId::Inbound(FAILING_NONCE);
		let amount = 1000;

		assert_ok!(EthereumSystemV2::add_tip(origin, sender.clone(), message_id.clone(), amount));

		System::assert_last_event(RuntimeEvent::EthereumSystemV2(Event::<Test>::TipProcessed {
			sender: sender.clone(),
			message_id,
			amount,
			success: false,
		}));

		let lost_tip = LostTips::<Test>::get(sender);
		assert_eq!(lost_tip, 1000);
	});
}
```
