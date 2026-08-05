Audit Report

## Title
Unbounded `u16` page-index counters in XCMP outbound queue overflow, permanently corrupting or halting message delivery to a sibling parachain - (File: cumulus/pallets/xcmp-queue/src/lib.rs)

## Summary
`OutboundChannelDetails` stores `first_index: u16` and `last_index: u16` as page cursors into `OutboundXcmpMessages`, and both fields are advanced with plain `+= 1` in `send_fragment` and `take_outbound_messages` with no `checked_add`/`saturating_add` and no reset for the lifetime of the channel. Because the only existing safeguard (`suspend_threshold`/`drop_threshold`) bounds the *difference* between the two counters, not their *absolute* values, the counters are free to march to `u16::MAX` over the cumulative lifetime of a channel and then overflow.

## Finding Description
`send_fragment()` creates a new outbound page whenever the existing page doesn't match or doesn't fit, incrementing the counter unconditionally:
```rust
let mut current_page = existing_page.unwrap_or_else(|| {
    channel_details.last_index += 1;
    ...
});
...
<OutboundXcmpMessages<T>>::insert(recipient, channel_details.last_index - 1, current_page);
``` [1](#0-0) 

`take_outbound_messages()` advances `first_index` in the same unguarded fashion when draining a page to the relay chain:
```rust
if last_index > first_index {
    let page = <OutboundXcmpMessages<T>>::get(*para_id, *first_index);
    if page.len() < max_size_now {
        <OutboundXcmpMessages<T>>::remove(*para_id, *first_index);
        *first_index += 1;
        ...
``` [2](#0-1) 

The only bound in the code, `page_count = last_index.saturating_sub(first_index)`, caps the instantaneous number of pages in flight, not the absolute counter values that both fields accumulate over the channel's entire lifetime [3](#0-2) . The struct definition and storage doc comment confirm the (incorrect) assumption that a `u16` is sufficient because "queues grow no greater than 65535 items" — that statement is only true for the momentary queue depth, not the cumulative index values [4](#0-3) . Once `last_index` reaches `65535` and wraps to `0` on the next new page, the subsequent `channel_details.last_index - 1` computation at the insert call underflows (since `last_index` is now `0`), and/or the newly wrapped index aliases an old, not-yet-drained key in `OutboundXcmpMessages`, corrupting or losing in-flight message pages, or panicking block execution if arithmetic overflow checks are active for the runtime build.

## Impact Explanation
This is reachable purely through repeated ordinary XCM sends (e.g., reserve transfers/teleports) that force new pages to be created on a given outbound channel — no privileged actor is required. The resulting failure mode is either (a) a panic in `send_fragment`/`take_outbound_messages`, executed from block-processing hooks, halting block production for the collator servicing that channel, or (b) silent index wraparound causing aliasing/loss of queued XCM pages in `OutboundXcmpMessages`, which can carry asset-transfer instructions. Both align with the accepted impact categories of "implementation bugs that can bring down... a Substrate-based chain" and "permanent user-fund or bridge-state lock" via message-queue corruption, matching the pivot that queue markers must only advance correctly and atomically.

## Likelihood Explanation
Reaching the `u16` boundary requires 65,536 cumulative new-page events on a single channel, which is a very large amount of aggregate traffic but is not gated by any privileged role — any account able to trigger XCM sends contributes, and nothing in the code resets or bounds the absolute counters. For long-lived, high-traffic parachain-to-parachain lanes this is a real, eventually-reachable design defect rather than a theoretical one, though it requires sustained traffic over a long period rather than a single transaction.

## Recommendation
Widen `first_index`/`last_index` to `u32`/`u64`, or replace the plain `+= 1` increments with `checked_add`/`saturating_add` combined with explicit channel suspension/rejection logic when the type's practical limit is approached, mirroring the `checked_add` + `Unsupported` rejection pattern already used for Snowbridge's outbound nonce [5](#0-4) .

## Proof of Concept
1. Over the lifetime of an outbound XCMP channel to parachain `P`, repeatedly submit XCM-sending extrinsics that force `send_fragment` to open a new page (e.g., large individual fragments or fragments requiring a different `XcmpMessageFormat`), each executing the unguarded `channel_details.last_index += 1;` [6](#0-5) .
2. As pages are relayed, `take_outbound_messages` executes the symmetric unguarded `*first_index += 1;` [7](#0-6) .
3. After 65,536 cumulative page-creation events, the next increment overflows `u16`, either panicking block execution or wrapping the index to `0`, causing `channel_details.last_index - 1` to underflow or alias a previously used, undrained `OutboundXcmpMessages` key, corrupting/losing queued messages for that parachain going forward.

### Citations

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L416-433)
```rust
/// Struct containing detailed information about the outbound channel.
#[derive(Clone, Eq, PartialEq, Encode, Decode, TypeInfo, Debug, MaxEncodedLen)]
pub struct OutboundChannelDetails {
	/// The `ParaId` of the parachain that this channel is connected with.
	recipient: ParaId,
	/// The state of the channel.
	state: OutboundState,
	/// Whether any signals exist in this channel.
	signals_exist: bool,
	/// The index of the first outbound message.
	first_index: u16,
	/// The index of the last outbound message.
	last_index: u16,
	/// Flags
	flags: OutboundChannelFlags,
	/// Cached total byte size of the pages currently queued in this channel.
	queued_bytes: u32,
}
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L611-629)
```rust
		let mut current_page = existing_page.unwrap_or_else(|| {
			// We need to add a new page.
			channel_details.last_index += 1;
			let new_page = format.encode();
			channel_details.queued_bytes =
				channel_details.queued_bytes.saturating_add(new_page.len() as u32);
			new_page
		});

		current_page.append(&mut encoded_fragment);
		channel_details.queued_bytes =
			channel_details.queued_bytes.saturating_add(encoded_fragment_len as u32);
		let current_page = WeakBoundedVec::try_from(current_page).map_err(|error| {
			tracing::debug!(target: LOG_TARGET, ?error, "Failed to create bounded message page");
			MessageSendError::TooBig
		})?;
		let page_count =
			channel_details.last_index.saturating_sub(channel_details.first_index) as u32;
		<OutboundXcmpMessages<T>>::insert(recipient, channel_details.last_index - 1, current_page);
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L1156-1163)
```rust
				if last_index > first_index {
					let page = <OutboundXcmpMessages<T>>::get(*para_id, *first_index);
					if page.len() < max_size_now {
						<OutboundXcmpMessages<T>>::remove(*para_id, *first_index);
						*first_index += 1;
						*queued_bytes = queued_bytes.saturating_sub(page.len() as u32);
						break 'page_fetch page;
					}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L381-388)
```rust
			let nonce = <Nonce<T>>::get().checked_add(1).ok_or_else(|| {
				Self::deposit_event(Event::MessageRejected {
					id: None,
					payload: message.to_vec(),
					error: Unsupported,
				});
				Unsupported
			})?;
```
