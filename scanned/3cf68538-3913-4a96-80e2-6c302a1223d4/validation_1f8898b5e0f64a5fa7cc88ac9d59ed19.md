[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2417-2432)
```rust
			let slash_weight =
				// apply slash if any before withdraw.
				match Self::do_apply_slash(&member_account, None, false) {
					Ok(_) => T::WeightInfo::apply_slash(),
					Err(e) => {
						let no_pending_slash: DispatchResult = Err(Error::<T>::NothingToSlash.into());
						// This is an expected error. We add appropriate fees and continue withdrawal.
						if Err(e) == no_pending_slash {
							T::WeightInfo::apply_slash_fail()
						} else {
							// defensive: if we can't apply slash for some reason, we abort.
							return Err(Error::<T>::Defensive(DefensiveError::SlashNotApplied).into());
						}
					}

				};
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

**File:** substrate/frame/message-queue/src/lib.rs (L1569-1587)
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
```

**File:** substrate/frame/staking-async/src/slashing.rs (L591-601)
```rust
	let mut ledger =
		match Pallet::<T>::ledger(sp_staking::StakingAccount::Stash(stash.clone())).defensive() {
			Ok(ledger) => ledger,
			Err(_) => return, // nothing to do.
		};

	let value = ledger.slash(value, asset::existential_deposit::<T>(), offence_era);
	if value.is_zero() {
		// nothing to do
		return;
	}
```
