### Title
Unincentivized `execute_overweight` allows permanently overweight XCMP/UMP/DMP messages to be silently reaped, permanently destroying bridged funds/state — ([File: substrate/frame/message-queue/src/lib.rs])

### Summary
`pallet-message-queue` — the pallet that backs XCMP, UMP and DMP message delivery across the whole Polkadot/Cumulus stack — automatically skips any message that exceeds the per-message overweight limit and marks it `OverweightEnqueued`. Unsticking such a message requires a *permissionless, unincentivized* call to `execute_overweight`, exactly the pattern described in the source report ("liquidation call with no incentive to be triggered"). If nobody calls `execute_overweight` before the page becomes "stale" (bounded by `MaxStale`), the page — and the message inside it, which may carry an XCM asset transfer or governance-relevant payload — is permanently reaped and the message is dropped forever with no execution and no compensation to anyone.

### Finding Description
The pallet's own docs describe the exact broken invariant: a permanently overweight message "will never be executed automatically through `on_initialize` nor by calling `service_queues`. Manual intervention in the form of `execute_overweight` is necessary" [1](#0-0) .

`process_message_payload` detects this condition and emits `OverweightEnqueued` while leaving the message parked in its page rather than executing it: [2](#0-1) .

The only way to advance this message is the signed, permissionless `execute_overweight` extrinsic: [3](#0-2) 

Unlike the Snowbridge `pallets/inbound-queue-v2` and `pallets/outbound-queue-v2`, which explicitly pay a `relayer_fee`/`tip` to whoever calls `process_message` / `process_delivery_receipt` [4](#0-3) [5](#0-4) , `execute_overweight` pays **no reward at all** to the caller, who must additionally supply/pay for `weight_limit` (the execution budget) themselves via normal transaction fees. There is no `Pays::No`, no tip, no fee refund analogous to `reap_stash`'s `Ok(Pays::No.into())` [6](#0-5) . This is precisely the "lack of incentives" bug class from the external report: a critical maintenance/settlement action that anyone can call, but nobody is economically motivated to call.

The consequence is worse than a delay: `Config::MaxStale` bounds how many stale (unprocessable/overweight) pages are tolerated before older ones are culled — the doc comment states plainly that "historical pages may be dropped, even if they contain unprocessed overweight messages" [7](#0-6) , and the pallet's own scenario docs confirm: "There is no guarantee that this will work since the message could be part of a stale page and be reaped before execution commences" [8](#0-7) . The `reap_page_permanent_overweight_works` test demonstrates this exact lifecycle: pages accumulate past `MaxStale`, and older stale pages containing permanently-overweight, never-executed messages get reaped via `do_reap_page` and vanish [9](#0-8) .

Because this pallet is the generic backbone for UMP (`polkadot/runtime/parachains/src/ump_tests.rs`) as well as DMP/XCMP on every Cumulus parachain runtime, an overweight UMP/DMP/XCMP message carrying, e.g., an XCM `DepositAsset`/teleport instruction that nobody bothers to manually execute (because there is no reward and it costs the caller gas) can be silently and permanently destroyed once enough subsequent messages push it past the stale-page watermark — a permanent loss of the funds/state that message represented, with no attacker action and no economic actor rewarded for preventing it.

### Impact Explanation
This is a systemic, chain-wide risk rather than a one-off bug: any Cumulus parachain or the relay chain itself relies on `pallet-message-queue` to deliver XCMP/UMP/DMP messages. If a message becomes permanently overweight (e.g. due to misestimated weights, a runtime upgrade changing weights, or a maliciously crafted heavy payload from a connected chain), it sits in `OverweightEnqueued` state indefinitely unless a third party spends their own funds to call `execute_overweight` for zero reward. Given no incentive exists, in periods of high message throughput the stale-page watermark can be reached and the page — with its un-executed message — is reaped and lost forever. If that message encoded an asset transfer/unlock, the underlying value is permanently locked/lost on the sending side while never credited on the receiving side, matching the "permanent user-fund lock" and "public underpriced work that … stalls bridge/message processing" impact classes.

### Likelihood Explanation
Likelihood is rated similarly to the source report (Low but plausible): it requires no attacker, malicious peer, or privileged actor — it is an emergent economic-incentive gap that manifests whenever weight estimation is imperfect (a routine occurrence across the ecosystem) and no altruistic party steps in to pay gas for zero reward before the stale-page watermark is reached. The bug's severity is High because the consequence (permanent message/fund loss) is irreversible and systemic across every chain using this pallet.

### Recommendation
Introduce an incentive mechanism for `execute_overweight` (and `reap_page`) analogous to what Snowbridge's inbound/outbound queues already do for relayers: pay the caller a portion of value tied to the message (e.g., a configurable tip/fee escrowed at enqueue time, or `Pays::No` plus a small treasury-funded reward), and/or raise `MaxStale`/extend backlog protection specifically for pages still holding permanently-overweight-but-unexecuted messages so they cannot be reaped before a bounded, sufficiently long grace period elapses. At minimum, ensure reaping of a stale page containing an un-executed overweight message emits a distinguishable event and, where the message pertains to bridged/locked value, ensure the source side is not treated as "delivered" until execution succeeds.

### Proof of Concept
Not applicable as a step-by-step exploit script — this is an economic-incentive/liveness defect provable directly from repository logic and existing tests, not from injecting malicious input. The relevant existing unit test `reap_page_permanent_overweight_works` [9](#0-8)  demonstrates the mechanics: pages are marked permanently overweight via `service_queues`, and once more than `MaxStale` pages accumulate, `do_reap_page` succeeds in deleting older stale pages that still contain their un-executed, permanently-overweight message — the same code path that would fire in production if no one calls `execute_overweight` in time, because no economic actor is rewarded for doing so.

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

**File:** substrate/frame/message-queue/src/lib.rs (L556-560)
```rust
		/// The maximum number of stale pages (i.e. of overweight messages) allowed before culling
		/// can happen. Once there are more stale pages than this, then historical pages may be
		/// dropped, even if they contain unprocessed overweight messages.
		#[pallet::constant]
		type MaxStale: Get<u32>;
```

**File:** substrate/frame/message-queue/src/lib.rs (L728-757)
```rust
		/// Execute an overweight message.
		///
		/// Temporary processing errors will be propagated whereas permanent errors are treated
		/// as success condition.
		///
		/// - `origin`: Must be `Signed`.
		/// - `message_origin`: The origin from which the message to be executed arrived.
		/// - `page`: The page in the queue in which the message to be executed is sitting.
		/// - `index`: The index into the queue of the message to be executed.
		/// - `weight_limit`: The maximum amount of weight allowed to be consumed in the execution
		///   of the message.
		///
		/// Benchmark complexity considerations: O(index + weight_limit).
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1805-1831)
```rust
		#[pallet::call_index(20)]
		#[pallet::weight(T::WeightInfo::reap_stash(*num_slashing_spans))]
		pub fn reap_stash(
			origin: OriginFor<T>,
			stash: T::AccountId,
			num_slashing_spans: u32,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// virtual stakers should not be allowed to be reaped.
			ensure!(!Self::is_virtual_staker(&stash), Error::<T>::VirtualStakerNotAllowed);

			let ed = asset::existential_deposit::<T>();
			let origin_balance = asset::total_balance::<T>(&stash);
			let ledger_total =
				Self::ledger(Stash(stash.clone())).map(|l| l.total).unwrap_or_default();
			let reapable = origin_balance < ed ||
				origin_balance.is_zero() ||
				ledger_total < ed ||
				ledger_total.is_zero();
			ensure!(reapable, Error::<T>::FundedTarget);

			// Remove all staking-related information and lock.
			Self::kill_stash(&stash, num_slashing_spans)?;

			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/message-queue/src/tests.rs (L311-349)
```rust
#[test]
fn reap_page_permanent_overweight_works() {
	use MessageOrigin::*;
	build_and_execute::<Test>(|| {
		// Create 10 pages more than the stale limit.
		let n = (MaxStale::get() + 10) as usize;
		for _ in 0..n {
			MessageQueue::enqueue_message(msg("weight=200 datadatadata"), Here);
		}
		assert_eq!(Pages::<Test>::iter().count(), n);
		assert_eq!(MessageQueue::footprint(Here).pages, n as u32);
		assert_eq!(QueueChanges::take().len(), n);
		// Mark all pages as stale since their message is permanently overweight.
		MessageQueue::service_queues(1.into_weight());

		// Check that we can reap everything below the watermark.
		let max_stale = MaxStale::get();
		for i in 0..n as u32 {
			let b = BookStateFor::<Test>::get(Here);
			let stale_pages = n as u32 - i;
			let overflow = stale_pages.saturating_sub(max_stale + 1) + 1;
			let backlog = (max_stale * max_stale / overflow).max(max_stale);
			let watermark = b.begin.saturating_sub(backlog);

			if i >= watermark {
				break;
			}
			assert_ok!(MessageQueue::do_reap_page(&Here, i));
			assert_eq!(QueueChanges::take(), vec![(Here, b.message_count - 1, b.size - 23)]);
		}

		// Cannot reap any more pages.
		for (o, i, _) in Pages::<Test>::iter() {
			assert_noop!(MessageQueue::do_reap_page(&o, i), Error::<Test>::NotReapable);
			assert!(QueueChanges::take().is_empty());
		}
		assert_eq!(MessageQueue::footprint(Here).pages, 3);
	});
}
```
