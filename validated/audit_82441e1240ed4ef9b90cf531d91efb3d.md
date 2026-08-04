Based on the evidence gathered, I've identified a strong local analog to the "challenge time window delay" bug class. It maps to `pallet-message-queue`'s overweight/staleness watermark, where the "grace window" before a stuck (permanently overweight) message is irrecoverably discarded can be forced to closer by an unprivileged attacker who floods the same queue with cheap overweight messages, causing a legitimate high-value message to be reaped (permanently dropped, unexecuted) before anyone can call the manual `execute_overweight` remedy — a direct parallel to "waiting/forcing the challenge window to run out before the proof/remedy lands."

### Title
Attacker-Inflated Stale-Page Watermark Causes Permanent, Unexecuted Loss of Overweight Messages in `pallet-message-queue` - (File: `substrate/frame/message-queue/src/lib.rs`)

### Summary
`pallet-message-queue` gives every "permanently overweight" message a grace period during which anyone can manually execute it via `execute_overweight` before its containing page becomes reapable and is dropped forever via the unprivileged `reap_page` call. The size of this grace period (the "watermark") is *not* a fixed time window — it dynamically shrinks as the number of stale (overweight) pages in the same queue grows [1](#0-0) . An unprivileged actor who can enqueue messages into a queue (e.g. via XCM/UMP/DMP) can deliberately stuff many cheap overweight messages into that same queue, mechanically pushing the watermark forward and forcing an earlier, legitimate overweight message (which may carry real value, e.g. an asset unlock, reward payout, or Transact call) to become reapable — and hence permanently discarded, unexecuted — before its beneficiary or an honest actor gets a chance to call `execute_overweight`. This is the direct structural analog of the reported "wait/race until end of challenge window" attack: instead of waiting, the attacker actively shortens the window through congestion.

### Finding Description
Message processing occurs in `service_queue`/`service_page`, and any message whose weight exceeds `overweight_limit` is marked permanently overweight and skipped, emitting `Event::OverweightEnqueued` [2](#0-1) . The pallet's own docs state this is only recoverable through manual, signed intervention: [3](#0-2) 

Recovery is via the signed, unprivileged `execute_overweight` call [4](#0-3) . But any signed account may also call `reap_page` to erase a page as soon as it becomes "reapable" or "cullable" [5](#0-4) .

The core of the vulnerable "window": `do_reap_page_inner` computes a dynamic `watermark` based on `stale_pages = total_pages - ready_pages` and `MaxStale`: [1](#0-0) 

Once `stale_pages` exceeds `MaxStale + 1`, `overflow` grows, `backlog = (max_stale * max_stale / overflow).max(max_stale)` shrinks, and `watermark = book_state.begin.saturating_sub(backlog)` moves *closer* to the present — meaning older stale pages (i.e., pages holding not-yet-executed overweight messages) fall below the watermark and become `cullable()`, hence reapable by anyone, regardless of whether the message was ever manually executed: [6](#0-5) 

Crucially, `do_reap_page_inner` never checks whether the overweight messages inside the page were actually processed — it only checks `page.remaining.is_zero()` (`reapable`) or the dynamic `cullable()` watermark. There is no requirement that `execute_overweight` be attempted first. This means:
1. The "challenge window" (time during which a stuck message can still be rescued via `execute_overweight`) is not a protocol-guaranteed fixed period — it is inversely proportional to how many stale/overweight pages currently exist in that same queue's book.
2. An attacker with cheap access to the same message origin/queue (e.g., anyone who can send UMP/DMP/XCM messages that are deliberately crafted to be overweight, or repeatedly trigger `Yield`/`Overweight` outcomes) can inflate `stale_pages` in that queue, shrinking `backlog`/`watermark`, and force an earlier page — potentially holding a legitimate, valuable overweight message — to cross into `cullable` territory before any honest party calls `execute_overweight`.
3. Once reaped, the message is unrecoverable: `Pages::<T>::remove(origin, page_index)` deletes it outright, with only a `PageReaped` event and no restitution path [7](#0-6) .

This breaks the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here, queue state (page removal) advances via `reap_page` without execution or settlement ever having occurred for the message that gets dropped.

### Impact Explanation
Because `pallet-message-queue` backs UMP/DMP dispatch queues (parachain-to-relay, relay-to-parachain) and other consumers (Snowbridge inbound queue, XCM Transact execution, etc.), a message that is permanently dropped without execution can represent an unbacked loss or permanent lock of value — e.g., an unclaimed asset unlock, a governance/Transact call that was supposed to move funds, or a bridge inbound settlement — with no way to replay it once reaped. This aligns with "permanent user-fund or bridge-state lock" and "public underpriced work that degrades block production or stalls bridge processing" in the accepted impact categories, and requires only an unprivileged attacker capable of enqueuing spam messages into the same queue/origin — no malicious validator, collator, relayer, or governance actor is needed.

### Likelihood Explanation
The attack requires:
- The ability to enqueue many cheap messages engineered to be permanently overweight into the *same* `MessageOriginOf<T>` queue as the target message (feasible for shared queues such as a parachain's UMP/DMP dispatch queue serving many senders), and
- Timing the flood before the legitimate rescuer calls `execute_overweight`.

Both preconditions are within reach of a normal, unprivileged chain user; no elevated permissions, validator/collator collusion, or off-chain infrastructure compromise are required. The complexity lies in correctly estimating `MaxStale`, `book_state.begin`, and the number of overweight pages needed to shrink the watermark past the target page — a computation, not a privilege.

### Recommendation
- Decouple "cullability" from the number of *unrelated* stale pages accumulated by third parties; e.g., require an explicit, fixed minimum number of blocks/pages of grace per individual overweight message regardless of how many other stale pages exist in the queue.
- Require at least one failed/attempted `execute_overweight` call (or an explicit "abandoned" flag with its own timeout) before a page containing overweight messages becomes reapable.
- Consider isolating "stale page" accounting per logical message class/value rather than allowing spam-driven inflation to affect the watermark for unrelated legitimate messages in the same queue.

### Proof of Concept
1. Party A sends message `M` (e.g., carrying an XCM `Transact`/asset unlock) into queue origin `O`; it is processed and found permanently overweight, emitting `OverweightEnqueued` and living in page `P` [2](#0-1) .
2. Attacker repeatedly enqueues throwaway messages into the same origin `O`, each engineered (large weight/`Yield`) to also become permanently overweight, each occupying a new stale page.
3. As stale page count for `O` climbs past `MaxStale + 1`, `overflow` increases and `backlog`/`watermark` in `do_reap_page_inner` shrinks (per the formula in lines 1183–1199), pulling page `P` below `watermark` before Party A or anyone else calls `execute_overweight` on `M`.
4. Attacker (or anyone) calls `reap_page(O, P)`; `cullable()` returns true, and `M` is deleted via `Pages::<T>::remove` without ever having been executed — funds/state tied to `M` are permanently lost, matching the behavior already anticipated (but not mitigated) in the module doc comment "There is no guarantee that this will work since the message could be part of a stale page and be reaped before execution commences." [3](#0-2)

### Citations

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

**File:** substrate/frame/message-queue/src/lib.rs (L716-726)
```rust
		/// Remove a page which has no more messages remaining to be processed or is stale.
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::reap_page())]
		pub fn reap_page(
			origin: OriginFor<T>,
			message_origin: MessageOriginOf<T>,
			page_index: PageIndex,
		) -> DispatchResult {
			ensure_signed(origin)?;
			Self::do_reap_page(&message_origin, page_index)
		}
```

**File:** substrate/frame/message-queue/src/lib.rs (L741-757)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(
			T::WeightInfo::execute_overweight_page_updated().max(
			T::WeightInfo::execute_overweight_page_removed()).saturating_add(*weight_limit)
		)]
		pub fn execute_overweight(
			origin: OriginFor<T>,
			message_origin: MessageOriginOf<T>,
			page: PageIndex,
			index: T::Size,
			weight_limit: Weight,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			let actual_weight =
				Self::do_execute_overweight(message_origin, page, index, weight_limit)?;
			Ok(Some(actual_weight).into())
		}
```

**File:** substrate/frame/message-queue/src/lib.rs (L1150-1213)
```rust
	/// Same as `do_reap_page` but must be called while holding the `service_mutex`.
	fn do_reap_page_inner(origin: &MessageOriginOf<T>, page_index: PageIndex) -> DispatchResult {
		let mut book_state = BookStateFor::<T>::get(origin);
		// definitely not reapable if the page's index is no less than the `begin`ning of ready
		// pages.
		ensure!(page_index < book_state.begin, Error::<T>::NotReapable);

		let page = Pages::<T>::get(origin, page_index).ok_or(Error::<T>::NoPage)?;

		// definitely reapable if the page has no messages in it.
		let reapable = page.remaining.is_zero();

		// also reapable if the page index has dropped below our watermark.
		let cullable = || {
			let total_pages = book_state.count;
			let ready_pages = book_state.end.saturating_sub(book_state.begin).min(total_pages);

			// The number of stale pages - i.e. pages which contain unprocessed overweight messages.
			// We would prefer to keep these around but will restrict how far into history they can
			// extend if we notice that there's too many of them.
			//
			// We don't know *where* in history these pages are so we use a dynamic formula which
			// reduces the historical time horizon as the stale pages pile up and increases it as
			// they reduce.
			let stale_pages = total_pages - ready_pages;

			// The maximum number of stale pages (i.e. of overweight messages) allowed before
			// culling can happen at all. Once there are more stale pages than this, then historical
			// pages may be dropped, even if they contain unprocessed overweight messages.
			let max_stale = T::MaxStale::get();

			// The amount beyond the maximum which are being used. If it's not beyond the maximum
			// then we exit now since no culling is needed.
			let overflow = match stale_pages.checked_sub(max_stale + 1) {
				Some(x) => x + 1,
				None => return false,
			};

			// The special formula which tells us how deep into index-history we will pages. As
			// the overflow is greater (and thus the need to drop items from storage is more urgent)
			// this is reduced, allowing a greater range of pages to be culled.
			// With a minimum `overflow` (`1`), this returns `max_stale ** 2`, indicating we only
			// cull beyond that number of indices deep into history.
			// At this overflow increases, our depth reduces down to a limit of `max_stale`. We
			// never want to reduce below this since this will certainly allow enough pages to be
			// culled in order to bring `overflow` back to zero.
			let backlog = (max_stale * max_stale / overflow).max(max_stale);

			let watermark = book_state.begin.saturating_sub(backlog);
			page_index < watermark
		};
		ensure!(reapable || cullable(), Error::<T>::NotReapable);

		Pages::<T>::remove(origin, page_index);
		debug_assert!(book_state.count > 0, "reaping a page implies there are pages");
		book_state.count.saturating_dec();
		book_state.message_count.saturating_reduce(page.remaining.into() as u64);
		book_state.size.saturating_reduce(page.remaining_size.into() as u64);
		BookStateFor::<T>::insert(origin, &book_state);
		T::QueueChangeHandler::on_queue_changed(origin.clone(), book_state.into());
		Self::deposit_event(Event::PageReaped { origin: origin.clone(), index: page_index });

		Ok(())
	}
```

**File:** substrate/frame/message-queue/src/lib.rs (L1589-1599)
```rust
		match transaction {
			Err(Overweight(w)) if w.any_gt(overweight_limit) => {
				// Permanently overweight.
				Self::deposit_event(Event::<T>::OverweightEnqueued {
					id,
					origin,
					page_index,
					message_index,
				});
				MessageExecutionStatus::Overweight
			},
```
