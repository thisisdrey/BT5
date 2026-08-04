### Title
Non-transactional handling of `Ok(false)` in `pallet-message-queue::process_message_payload` permanently drops messages while committing their partial side effects - (File: `substrate/frame/message-queue/src/lib.rs`)

### Summary
`pallet-message-queue` processes queued messages through `T::MessageProcessor::process_message()`, wraps that call in a storage transaction, but only rolls back on `Err(_)`. A processor that returns `Ok(false)` ("processed" flag `false`, meaning it did not fully succeed but did not error) has its storage side effects **committed**, while the outer state machine treats the return value as `MessageExecutionStatus::Processed` regardless of the inner `success` bool, permanently removing the message from the queue. This mirrors the reported Solidity bug class: a `success`/return-value signal from an inner call is not correctly propagated to prevent finalizing state, so partial execution gets silently accepted as "done" with no way to retry.

### Finding Description
In `process_message_payload`, the message processor's result is wrapped in `storage::with_transaction`: [1](#0-0) 

Only `Err(_)` triggers `TransactionOutcome::Rollback`; any `Ok(_)` — including `Ok(false)` — is committed: [2](#0-1) 

Further down, the `Ok(success)` branch discards the `success` value entirely and always maps to `MessageExecutionStatus::Processed`: [3](#0-2) 

`service_page_item` then treats `Processed` as `is_processed = true` and calls `page.skip_first(true)`, permanently advancing past the message so it can never be retried: [4](#0-3) 

This is not accidental — the `prdoc` for this exact change documents the intended semantics: "`Ok(false)` will not revert, only `Err(_)`" (i.e., the pallet explicitly chose to commit-and-finalize on `Ok(false)`) rather than treat it symmetrically with genuine unprocessable/error states: [5](#0-4) 

Any `ProcessMessage` implementation that can legitimately mutate storage (withdraw funds, decrement balances, update nonces, etc.) *before* determining full success, and that reports `Ok(false)` on partial failure instead of `Err(_)`, will have those mutations become permanent while the message itself is discarded as "processed" — exactly the H-27 pattern of a caller not treating a callee's failure/incomplete signal as a reason to prevent finalizing state.

### Impact Explanation
Because message removal (`Processed`) is decoupled from whether the inner processor actually succeeded, a `ProcessMessage` implementation that performs partial storage writes and returns `Ok(false)` on failure (rather than an `Err`) will:
- Commit those partial writes (funds moved/burned/locked, counters incremented, etc.).
- Permanently remove the message from the queue (`skip_first(true)`), so `execute_overweight`/reprocessing can never recover or complete the operation.

This matches the "permanent user-fund or bridge-state lock" and "message queues... must only advance after decode, dispatch, execution, and settlement succeed atomically" pivots: the queue advances on `Ok(false)` even though settlement did not succeed, and any state mutated in that attempt is not rolled back.

### Likelihood Explanation
This is a systemic pallet-level design decision, not a hypothetical: any `ProcessMessage` implementation registered as `Config::MessageProcessor` for `pallet-message-queue` in a Polkadot-SDK-based runtime inherits this exact behavior. `ProcessXcmMessage` (the most common implementation, used for DMP/XCMP processing) is unaffected here because XCM's `post_process` explicitly traps leftover holding assets on `Incomplete` (verified by `deposit_assets_with_retry_aborts_on_failure_and_post_process_traps`), so it is not itself a full analog. But the message-queue's transactional guarantee is documented and general-purpose, and it is only maintained for `Err`, not for `Ok(false)`. Any custom or future `MessageProcessor` (parachain-specific message handlers, non-XCM DMP/HRMP consumers) that performs multi-step storage mutation and can return `Ok(false)` without also being written to be as defensively idempotent/trap-based as `ProcessXcmMessage` would silently commit inconsistent partial state and drop the message forever. The risk is architectural (a footgun baked into the pallet's transactional contract) rather than a currently-exploitable path in the shipped `ProcessXcmMessage` implementation, so likelihood is moderate rather than immediately provable end-to-end for the default runtime configuration.

### Recommendation
- Treat `Ok(false)` (non-permanent unprocessable/incomplete) the same as an error for rollback purposes unless the specific `MessageProcessor` implementation guarantees that partial state left behind on `Ok(false)` is safe/recoverable (as XCM does via asset trapping).
- Alternatively, require `ProcessMessage` implementations to explicitly document/enforce that `Ok(false)` never leaves unrecoverable partial storage mutations, and add a `try_state`/`do_try_state` invariant check or an explicit rollback flag distinguishing "safely partial" vs. "needs rollback" outcomes.
- Add a defensive test harness/lint that fails CI if a `ProcessMessage` impl mutates storage and returns `Ok(false)` without corresponding compensating/trap logic.

### Proof of Concept
Conceptual reproduction, following `substrate/frame/message-queue/src/tests.rs` patterns:
1. Implement a mock `ProcessMessage` whose `process_message` performs a storage write (e.g., decrements a `Balances` entry / marks a "fund moved" flag) and then, upon hitting a simulated downstream failure, returns `Ok(false)` instead of `Err(ProcessMessageError::Yield)`.
2. Enqueue a message and call `MessageQueue::service_queues`.
3. Observe:
   - The storage write from step 1 persists (transaction committed because `res` was `Ok(_)`), confirmed via the commit/rollback logic at [1](#0-0) .
   - `Event::Processed { success: false, .. }` fires, but the message is treated as fully `Processed` and removed from the page (`page.skip_first(true)`), per [4](#0-3) .
   - No `execute_overweight`/retry path exists to recover the message, since it is no longer stored as overweight or pending.

This demonstrates the same class of bug as H-27: a callee's failure signal (`false`/`success == false`) is not converted into a revert, so partial mutations are committed and finalization proceeds as if the operation fully succeeded, with no remaining path to reconcile or retry.

### Citations

**File:** substrate/frame/message-queue/src/lib.rs (L1378-1391)
```rust
		let is_processed = match res {
			InsufficientWeight => return ItemExecutionStatus::Bailed,
			Unprocessable { permanent: false } => return ItemExecutionStatus::NoProgress,
			Processed | Unprocessable { permanent: true } | StackLimitReached => true,
			Overweight => false,
		};

		if is_processed {
			book_state.message_count.saturating_dec();
			book_state.size.saturating_reduce(payload_len as u64);
		}
		page.skip_first(is_processed);
		ItemExecutionStatus::Executed(is_processed)
	}
```

**File:** substrate/frame/message-queue/src/lib.rs (L1569-1577)
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
```

**File:** substrate/frame/message-queue/src/lib.rs (L1618-1628)
```rust
			Ok(success) => {
				// Success
				let weight_used = meter.consumed().saturating_sub(prev_consumed);
				Self::deposit_event(Event::<T>::Processed {
					id: id.into(),
					origin,
					weight_used,
					success,
				});
				MessageExecutionStatus::Processed
			},
```

**File:** prdoc/stable2412/pr_5198.prdoc (L1-13)
```text
title: "MQ processor should be transactional"

doc:
  - audience: [Runtime User, Runtime Dev]
    description: |
      Enforce transactional processing on pallet Message Queue Processor.

      Storage changes that were done while processing a message will now be rolled back
      when the processing returns an error. `Ok(false)` will not revert, only `Err(_)`.

crates:
  - name: pallet-message-queue
    bump: major
```
