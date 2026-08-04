### Title
Inbound XCMP channel signalled `Suspend` to sibling but never recorded as suspended when `MaxInboundSuspended` bound is reached, permanently preventing the `Resume` signal from ever being sent - ([File: cumulus/pallets/xcmp-queue/src/lib.rs])

### Summary
`cumulus_pallet_xcmp_queue`'s congestion control mirrors the reported pattern exactly: an action that changes external/remote state (sending a `ChannelSignal::Suspend`/`Resume` message to a sibling parachain, analogous to revoking/granting the `MINTER_ROLE`/`BURNER_ROLE`) is not always accompanied by the corresponding local boolean bookkeeping update (`InboundXcmpSuspended`, analogous to `collateralActive`). When the bookkeeping update fails, the guard that decides whether to resume a channel (`suspended && fp.ready_pages <= resume_threshold`) can never become true again for that sibling, so the sibling’s inbound channel is signalled to suspend but is never signalled to resume.

### Finding Description
`OnQueueChanged::on_queue_changed` in [1](#0-0)  drives suspend/resume decisions purely from the `InboundXcmpSuspended` bounded set:

```rust
let mut suspended_channels = <InboundXcmpSuspended<T>>::get();
let suspended = suspended_channels.contains(&para);

if suspended && fp.ready_pages <= resume_threshold {
    // send Resume signal, then remove from suspended_channels
} else if !suspended && fp.ready_pages >= suspend_threshold {
    // send Suspend signal, then try_insert into suspended_channels
}
```

`InboundXcmpSuspended` is declared as a bounded set:
```rust
pub type InboundXcmpSuspended<T: Config> =
    StorageValue<_, BoundedBTreeSet<ParaId, T::MaxInboundSuspended>, ValueQuery>;
``` [2](#0-1) 

In the “suspend” branch, the code first calls `Self::send_signal(para, ChannelSignal::Suspend)` — which unconditionally succeeds at queuing an actual outbound `Suspend` XCMP signal to the sibling (this is the side-effect that changes the remote party's behavior, just like `revokeRole` in the Solidity bug) — and *only afterwards* attempts `suspended_channels.try_insert(para)`. If the bounded set is already at `T::MaxInboundSuspended` capacity, `try_insert` fails, and the code merely logs an error and returns without updating `InboundXcmpSuspended`:

```rust
if let Err(err) = Self::send_signal(para, ChannelSignal::Suspend) {
    ...
} else if let Err(err) = suspended_channels.try_insert(para) {
    tracing::error!(..., "Too many channels suspended; cannot suspend sibling; further messages may be dropped.");
} else {
    <InboundXcmpSuspended<T>>::put(suspended_channels);
}
``` [3](#0-2) 

At this point the remote sibling has already received the `Suspend` signal and will stop pushing new XCMP messages to this chain for `para`, but the local `suspended` flag for `para` is `false` (it was never added to the set). On subsequent invocations of `on_queue_changed` for the same `para`:
- The "resume" branch requires `suspended == true`, which will never be satisfied for this `para`, so `ChannelSignal::Resume` is never sent, no matter how much the queue drains.
- The "suspend" branch keeps re-triggering (`!suspended && ready_pages >= suspend_threshold`) and keeps re-sending redundant `Suspend` signals (each of which succeeds again on the remote side), but the local set insertion keeps failing under sustained congestion from many siblings.

This is structurally identical to the reported Solidity bug: `disableCollateral` revokes roles (real side-effect) without updating `collateralActive` (bookkeeping), and the asymmetric guard in `enableCollateral` (`!collateralActive[_collateral]`) then permanently blocks re-enabling. Here, the real side-effect (`Suspend` signal delivered) proceeds while the bookkeeping (`InboundXcmpSuspended`) silently fails to update, and the guard in the resume path (`suspended && ...`) can never fire for that sibling again.

### Impact Explanation
This can permanently stall XCMP message delivery from an unrelated, otherwise-healthy sibling parachain that has done nothing wrong, once too many other siblings are congested simultaneously (`MaxInboundSuspended` exceeded). Because the local chain never sends the corresponding `Resume` signal, the sibling’s outbound HRMP channel toward this chain remains suspended indefinitely — degrading or halting cross-chain message flow (assets, XCM instructions, bridge-relevant traffic through bridge-hub runtimes) without any governance/root action to fix it, matching the "public underpriced work that degrades block production or stalls bridge processing" / "message queues... must only advance after ... execution ... succeed atomically" impact criteria. No malicious relayer, validator, or privileged actor is required — the trigger is simply organic or attacker-induced message congestion from multiple sibling chains.

### Likelihood Explanation
Requires the number of simultaneously-congested sibling channels (each with `fp.ready_pages >= suspend_threshold`) to exceed `T::MaxInboundSuspended` at the same time. This bound is a fixed, typically small runtime constant (present in asset-hub, bridge-hub, collectives, coretime, people runtimes per `MaxInboundSuspended` grep hits), so on a busy network with many parachains, or under deliberate flooding from several sender parachains toward a shared destination, this condition is plausible without any privileged capability — an attacker only needs the ability to send enough XCMP traffic from several origins to push several channels over `suspend_threshold` concurrently.

### Recommendation
Update `InboundXcmpSuspended` and the decision to send a `Suspend`/`Resume` signal atomically, or invert the order: first reserve capacity (or check `try_insert` would succeed) before sending the `Suspend` signal, so the local bookkeeping and the remote signal are always consistent. Alternatively, on `try_insert` failure, avoid sending `Suspend` at all (or retry insertion on a future block until it can be recorded, and correspondingly track "signal sent but not recorded" state so a `Resume` can eventually still be issued once capacity frees up).

### Proof of Concept
1. Deploy a runtime with a small `MaxInboundSuspended` (as configured in the affected parachain runtimes).
2. From N > `MaxInboundSuspended` distinct sibling parachains, send enough XCMP messages toward the target chain so that each sibling’s inbound queue `fp.ready_pages` reaches `suspend_threshold` in the same block/sequence of blocks.
3. Observe (via `cumulus/pallets/xcmp-queue/src/lib.rs::on_queue_changed`) that for the siblings beyond the `MaxInboundSuspended` capacity, `send_signal(para, ChannelSignal::Suspend)` succeeds (an outbound `Suspend` XCMP signal is enqueued to that sibling) but `suspended_channels.try_insert(para)` fails and `InboundXcmpSuspended` is not updated for that `para`.
4. Let the queue for that `para` drain below `resume_threshold`. Confirm that `on_queue_changed`’s "resume" branch never fires for that `para` because `suspended_channels.contains(&para)` is `false`, so no `ChannelSignal::Resume` is ever sent — the sibling’s outbound channel toward this chain remains suspended indefinitely.

### Citations

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L315-325)
```rust
	/// The suspended inbound XCMP channels. All others are not suspended.
	///
	/// This is a `StorageValue` instead of a `StorageMap` since we expect multiple reads per block
	/// to different keys with a one byte payload. The access to `BoundedBTreeSet` will be cached
	/// within the block and therefore only included once in the proof size.
	///
	/// NOTE: The PoV benchmarking cannot know this and will over-estimate, but the actual proof
	/// will be smaller.
	#[pallet::storage]
	pub type InboundXcmpSuspended<T: Config> =
		StorageValue<_, BoundedBTreeSet<ParaId, T::MaxInboundSuspended>, ValueQuery>;
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L879-919)
```rust
impl<T: Config> OnQueueChanged<ParaId> for Pallet<T> {
	// Suspends/Resumes the queue when certain thresholds are reached.
	fn on_queue_changed(para: ParaId, fp: QueueFootprint) {
		let QueueConfigData { resume_threshold, suspend_threshold, .. } = <QueueConfig<T>>::get();

		let mut suspended_channels = <InboundXcmpSuspended<T>>::get();
		let suspended = suspended_channels.contains(&para);

		if suspended && fp.ready_pages <= resume_threshold {
			if let Err(err) = Self::send_signal(para, ChannelSignal::Resume) {
				tracing::error!(
					target: LOG_TARGET,
					error=?err,
					sibling=?para,
					"defensive: Could not send resumption signal to inbound channel of sibling; channel remains suspended."
				);
			} else {
				suspended_channels.remove(&para);
				<InboundXcmpSuspended<T>>::put(suspended_channels);
			}
		} else if !suspended && fp.ready_pages >= suspend_threshold {
			tracing::warn!(target: LOG_TARGET, sibling=?para, "XCMP queue for sibling is full; suspending channel.");

			if let Err(err) = Self::send_signal(para, ChannelSignal::Suspend) {
				// It will retry if `drop_threshold` is not reached, but it could be too late.
				tracing::error!(
					target: LOG_TARGET, error=?err,
					"defensive: Could not send suspension signal; future messages may be dropped."
				);
			} else if let Err(err) = suspended_channels.try_insert(para) {
				tracing::error!(
					target: LOG_TARGET,
					error=?err,
					sibling=?para,
					"Too many channels suspended; cannot suspend sibling; further messages may be dropped."
				);
			} else {
				<InboundXcmpSuspended<T>>::put(suspended_channels);
			}
		}
	}
```
