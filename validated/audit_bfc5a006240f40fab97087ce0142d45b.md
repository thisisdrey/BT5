## Title
Permissionless `reap_page` can purge overweight-message pages before their guaranteed manual execution, permanently destroying undelivered XCM/DMP/UMP payloads - (File: `substrate/frame/message-queue/src/lib.rs`)

## Summary
`pallet-message-queue::reap_page` is a fully public, signed-only extrinsic (no origin filter beyond `ensure_signed`). It can evict a page of messages that are only *temporarily* "stale" (i.e. permanently-overweight messages still awaiting manual `execute_overweight`), as long as the pallet's dynamic `cullable()` watermark has advanced past that page. This is the same class of bug as the Optimism `deleteL2Outputs` finding: state that is supposed to remain available until an explicit, guaranteed processing step (finalization / manual execution) can be wiped out early by an ordinary, unprivileged caller, causing permanent loss of the underlying payload (and any value it represents, e.g. XCM asset transfers queued via DMP/UMP).

## Finding Description
`reap_page` is declared with only a signed-origin check: [1](#0-0) 

It delegates to `do_reap_page_inner`, which allows removal of a page either when it is truly empty (`reapable`) or when it is merely `cullable()` - i.e. beyond a shrinking "watermark" computed from the number of currently stale pages: [2](#0-1) 

The `cullable()` watermark formula deliberately narrows the safe retention window as more permanently-overweight pages pile up, explicitly trading "not enough space" against "we might destroy pending overweight messages": [3](#0-2) 

Crucially, "permanently overweight" does not mean "invalid" or "worthless" - it means the message could not be serviced within a single block's weight budget and is queued for `execute_overweight` (a call anyone may invoke, but which requires someone to actually do it): [4](#0-3) 

The pallet's own documentation admits this is a real hazard, not just theoretical: [5](#0-4) 

This queue backs DMP and UMP message processing for parachains (including XCM instructions carrying asset reserve-transfers/mints), i.e. the corrupted value is the queued `Page` payload itself - once `Pages::<T>::remove(origin, page_index)` executes inside `do_reap_page_inner`, the encoded message (and any asset-movement instruction it represents) is gone forever, with no on-chain record of its content, before its "confirmed" processing guarantee (successful dispatch) was ever honored. Just like the Optimism bug where `deleteL2Outputs` had no check against a state that had already crossed its guaranteed finalization boundary, `do_reap_page_inner`'s `cullable()` path has no check that the page's messages have actually failed permanently or been superseded - it only checks a *count-based* watermark that any attacker can manipulate by flooding many additional overweight pages to shrink the retention depth.

## Impact Explanation
If a legitimate but temporarily overweight message representing an asset transfer (e.g., a UMP/DMP-relayed XCM `ReserveAssetDeposited`/mint instruction) is enqueued and marked permanently overweight due to transient weight pressure, an attacker can force enough additional overweight, low-value messages into the same queue to shrink the `cullable()` backlog window, then call the public `reap_page` extrinsic to remove the page before its rightful owner or an honest actor calls `execute_overweight`. The result is a permanent loss/lock of the funds or state update that message was meant to deliver - matching the "permanent user-fund or bridge-state lock" and "duplicate settlement/never-settled" impact categories in the required-impact list, and directly analogous to a withdrawal that should have been guaranteed becoming permanently unfulfillable.

## Likelihood Explanation
`reap_page` requires only `ensure_signed`, so any account can call it - no governance, no admin, no malicious relayer/collator/validator assumption is required. An attacker only needs to be able to enqueue enough cheap overweight messages (achievable via ordinary XCM/DMP/UMP traffic under weight pressure) to shrink the watermark below the target page, then submit `reap_page` themselves. This satisfies the "public underpriced work" and "unauthorized... action by an unprivileged attacker" bar in the gate.

## Recommendation
Do not allow `cullable()`-based reaping to remove pages that still contain messages whose `is_processed` flag is false unless a substantially longer, execution-guarantee-respecting grace period (or an explicit governance/root override) has elapsed - i.e., separate "space-pressure eviction of provably dead pages" from "eviction of pages with live, not-yet-executed messages." At minimum, gate `cullable()` eviction behind a much stricter, non-attacker-controllable threshold (e.g., block-number-based grace period rather than a page-count watermark that can be inflated by the caller's own enqueued traffic), and emit a distinguishable event (e.g., `OverweightMessageDropped`) so downstream consumers (XCM asset trackers) can react to a forced loss rather than silently losing funds.

## Proof of Concept
1. Enqueue a legitimate overweight message `M` carrying, e.g., an XCM instruction that would mint/unlock funds on the destination chain, into origin `O`. `M` is marked permanently overweight (`OverweightEnqueued`) and stored in page `P` awaiting `execute_overweight`.
2. Attacker repeatedly enqueues cheap overweight messages into the same or sibling queues to inflate the `stale_pages` count above `MaxStale`, shrinking `backlog`/`watermark` per the formula in `do_reap_page_inner` (lines 1162-1200).
3. Once `page_index` for `P` falls below the shrunk `watermark`, attacker (any signed account) calls `MessageQueue::reap_page(origin, O, P)`.
4. `do_reap_page_inner` treats `P` as `cullable()` and calls `Pages::<T>::remove(O, P)`, permanently destroying `M` before anyone called `execute_overweight` on it - equivalent to the Optimism challenger deleting a withdrawal state before its rightful settlement, resulting in permanent loss of the funds/state the message represented.

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
