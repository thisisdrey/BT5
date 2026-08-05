This confirms the key mechanism: `pallet-scheduler` docs explicitly note that scheduled calls dispatched via `Scheduler::schedule`/`schedule_named` are still subject to `frame_system::Config::BaseCallFilter` **for all origins except Root** [1](#0-0) . `pallet-referenda`'s `nudge_referendum`/`one_fewer_deciding` alarms are scheduled through `T::Scheduler` and, since the runtime's `SafeModeWhitelistedCalls`/`TxPauseWhitelistedCalls` only allow `System`, `SafeMode`, `TxPause` (and `Balances::transfer_keep_alive`), a Root-origin alarm would still fire even while the protocol is paused, but the underlying `service_referendum` clock (`decision_period`, `prepare_period`, `T::UndecidingTimeout`) advances strictly with the raw block number [2](#0-1) [3](#0-2) . Meanwhile `Referenda::vote`, `Referenda::place_decision_deposit`, etc. are ordinary signed extrinsics that ARE filtered by `BaseCallFilter = InsideBoth<SafeMode, TxPause>` and will return `CallFiltered` while the chain is paused [4](#0-3) [5](#0-4) .

### Title
Referendum decision/timeout clocks keep advancing during SafeMode/TxPause, causing legitimate referenda to time-out or fail while voting is blocked - (File: `substrate/frame/referenda/src/lib.rs`)

### Summary
This is a direct local analog of the report: users have a fixed-length "window" (`prepare_period` / `decision_period` / `T::UndecidingTimeout`) to act (vote, deposit) on a referendum, but if the runtime enters SafeMode or a TxPause is active for the relevant calls, users cannot vote or place a decision deposit. The referenda alarm mechanism itself is unaffected because it is driven by Root-origin scheduled calls, so `service_referendum` keeps advancing the wall-clock windows and can conclude the referendum (`TimedOut`/`Rejected`) exactly while normal users were blocked from acting — with no adjustment for the paused period, mirroring the reported bug where "the pausing mechanism does not extend the window time frame."

### Finding Description
`service_referendum` computes state transitions purely from `T::BlockNumberProvider::current_block_number()` versus track constants:
- Preparation deadline: `status.submitted + track.prepare_period` [6](#0-5) 
- Non-deciding timeout: `status.submitted + T::UndecidingTimeout::get()` [7](#0-6) [8](#0-7) 
- Decision-period rejection deadline: `deciding.since.saturating_add(track.decision_period)` [9](#0-8) 

None of these deadlines are paused or extended when the runtime's `BaseCallFilter` blocks user-facing dispatchables. The alarms that drive `service_referendum` forward (`nudge_referendum`, `one_fewer_deciding`) are scheduled via `T::Scheduler` and, per the scheduler's own documentation, calls dispatched from the scheduler bypass `BaseCallFilter` when the origin is Root [1](#0-0) , and `Referenda::note_one_fewer_deciding`/`ensure_alarm_at` construct these calls and dispatch them internally regardless of pause state [10](#0-9) . Meanwhile the only way an ordinary account can influence the tally or push a referendum past a pending deadline — `vote`, `place_decision_deposit` — are top-level signed extrinsics subject to the runtime's `InsideBoth<SafeMode, TxPause>` filter [4](#0-3) , and those pallets are not in either whitelist (`SafeModeWhitelistedCalls` only allows `System`/`SafeMode`/`TxPause`; `TxPauseWhitelistedCalls` only allows `Balances::transfer_keep_alive`) [11](#0-10) .

Consequently, if SafeMode is triggered (which is permissionlessly triggerable by anyone depositing `EnterDepositAmount`, per `SafeMode::enter`) for a duration comparable to or exceeding a track's `prepare_period`/`decision_period`/`UndecidingTimeout`, active referenda can silently transition to `TimedOut` (no deposit is returned, only `submission_deposit`/`decision_deposit` refunds happen per the specific branch) or `Rejected` purely because the electorate could not vote during the outage — exactly analogous to the reported staking/lending "missed withdrawal window" bug.

### Impact Explanation
This can cause legitimate governance proposals to be unfairly rejected/timed-out due to an outage window they had no way to react to, and — since `TimedOut`/`Rejected` are final states — the referendum must be resubmitted, wasting the submission deposit's opportunity cost and disrupting protocol governance timing. This falls under "runtime bugs that compromise intended behavior" for governance dispatch flow, since the intended guarantee (the community gets the full configured decision/confirm window to influence the outcome) is silently violated whenever an unprivileged, permissionless SafeMode activation coincides with an active referendum.

### Likelihood Explanation
`pallet_safe_mode::enter` is callable by any signed account that can afford the deposit, with no permission check [12](#0-11) , and its duration (`EnterDuration`, extendable via permissionless `extend`) is a runtime constant independent of any in-flight referenda's remaining window. Any unprivileged user can trigger this precondition; no admin, validator, or governance action is required to create the race condition, making this directly reachable rather than merely theoretical.

### Recommendation
Track cumulative "paused" block time (e.g., via a `SafeModeNotify`/`TransactionPause` hook) and either (a) freeze `service_referendum`'s alarms/deadlines while the runtime is in SafeMode or the relevant calls are tx-paused, extending `prepare_period`, `decision_period`, `confirm_period`, and `T::UndecidingTimeout` by the paused duration, or (b) whitelist governance-critical calls (`vote`, `place_decision_deposit`) in `SafeModeWhitelistedCalls`/`TxPauseWhitelistedCalls` so voting always remains possible even while other calls are paused.

### Proof of Concept
1. Configure a track with a short `decision_period` (e.g. a few hundred blocks, as in the Westend `monetary_guard`/`referendum_canceller` tracks) [13](#0-12) .
2. Submit a referendum and let it enter the deciding phase (`status.deciding.since = N`).
3. Any account calls `SafeMode::enter` (permissionless, deposit-only) shortly before the tally would otherwise flip to passing, keeping `Referenda::vote`/`place_decision_deposit` filtered for `EnterDuration` blocks.
4. Because `service_referendum`'s rejection check `now >= deciding.since.saturating_add(track.decision_period)` is unaffected by SafeMode, the referendum is auto-`Rejected` once `decision_period` elapses, even though voters were blocked from casting votes during that window [9](#0-8) .
5. Once SafeMode exits, voters can call `vote` again, but the referendum is already concluded — mirroring the report's "user misses withdrawal window while protocol was paused" scenario.

Note: I was unable to fully confirm within this session whether Root-origin scheduler dispatch unconditionally bypasses `BaseCallFilter` at the `frame_system`/`frame_executive` level in this exact codebase revision (the scheduler pallet's own doc comment states this behavior, but I could not trace the precise `apply_call_filter` implementation in `substrate/frame/system/src/lib.rs` before running out of tool calls). If Root-origin scheduled calls are in fact also filtered, the alarm-driven progression itself would stall during the pause, which would change (but not eliminate) the underlying analog — a background engineer should verify this filter-bypass behavior before treating the timeline above as exact.

### Citations

**File:** substrate/frame/scheduler/src/lib.rs (L36-42)
```rust
//! __NOTE:__ Instead of using the filter contained in the origin to call `fn schedule`, scheduled
//! runtime calls will be dispatched with the default filter for the origin: namely
//! `frame_system::Config::BaseCallFilter` for all origin types (except root which will get no
//! filter).
//!
//! If a call is scheduled using proxy or whatever mechanism which adds filter, then those filter
//! will not be used when dispatching the schedule runtime call.
```

**File:** substrate/frame/referenda/src/lib.rs (L1038-1086)
```rust
	/// Schedule a call to `one_fewer_deciding` function via the dispatchable
	/// `defer_one_fewer_deciding`. We could theoretically call it immediately (and it would be
	/// overall more efficient), however the weights become rather less easy to measure.
	fn note_one_fewer_deciding(track: TrackIdOf<T, I>) {
		// Set an alarm call for the next block to nudge the track along.
		let now = T::BlockNumberProvider::current_block_number();
		let next_block = now + One::one();
		let call = match T::Preimages::bound(CallOf::<T, I>::from(Call::one_fewer_deciding {
			track,
		})) {
			Ok(c) => c,
			Err(_) => {
				debug_assert!(false, "Unable to create a bounded call from `one_fewer_deciding`??",);
				return;
			},
		};
		Self::set_alarm(call, next_block);
	}

	/// Ensure that a `service_referendum` alarm happens for the referendum `index` at `alarm`.
	///
	/// This will do nothing if the alarm is already set.
	///
	/// Returns `false` if nothing changed.
	fn ensure_alarm_at(
		status: &mut ReferendumStatusOf<T, I>,
		index: ReferendumIndex,
		alarm: BlockNumberFor<T, I>,
	) -> bool {
		if status.alarm.as_ref().map_or(true, |&(when, _)| when != alarm) {
			// Either no alarm or one that was different
			Self::ensure_no_alarm(status);
			let call =
				match T::Preimages::bound(CallOf::<T, I>::from(Call::nudge_referendum { index })) {
					Ok(c) => c,
					Err(_) => {
						debug_assert!(
							false,
							"Unable to create a bounded call from `nudge_referendum`??",
						);
						return false;
					},
				};
			status.alarm = Self::set_alarm(call, alarm);
			true
		} else {
			false
		}
	}
```

**File:** substrate/frame/referenda/src/lib.rs (L1109-1181)
```rust
	fn service_referendum(
		now: BlockNumberFor<T, I>,
		index: ReferendumIndex,
		mut status: ReferendumStatusOf<T, I>,
	) -> (ReferendumInfoOf<T, I>, bool, ServiceBranch) {
		let mut dirty = false;
		// Should it begin being decided?
		let track = match T::Tracks::info(status.track) {
			Some(x) => x,
			None => return (ReferendumInfo::Ongoing(status), false, ServiceBranch::Fail),
		};
		// Default the alarm to the end of the world.
		let timeout = status.submitted + T::UndecidingTimeout::get();
		let mut alarm = BlockNumberFor::<T, I>::max_value();
		let branch;
		match &mut status.deciding {
			None => {
				// Are we already queued for deciding?
				if status.in_queue {
					// Does our position in the queue need updating?
					let ayes = status.tally.ayes(status.track);
					let mut queue = TrackQueue::<T, I>::get(status.track);
					let maybe_old_pos = queue.iter().position(|(x, _)| *x == index);
					let new_pos = queue.binary_search_by_key(&ayes, |x| x.1).unwrap_or_else(|x| x);
					branch = if maybe_old_pos.is_none() && new_pos > 0 {
						// Just insert.
						let _ = queue.force_insert_keep_right(new_pos, (index, ayes));
						ServiceBranch::RequeuedInsertion
					} else if let Some(old_pos) = maybe_old_pos {
						// We were in the queue - slide into the correct position.
						queue[old_pos].1 = ayes;
						queue.slide(old_pos, new_pos);
						ServiceBranch::RequeuedSlide
					} else {
						ServiceBranch::NotQueued
					};
					TrackQueue::<T, I>::insert(status.track, queue);
				} else {
					// Are we ready for deciding?
					branch = if status.decision_deposit.is_some() {
						let prepare_end = status.submitted.saturating_add(track.prepare_period);
						if now >= prepare_end {
							let (maybe_alarm, branch) =
								Self::ready_for_deciding(now, &track, index, &mut status);
							if let Some(set_alarm) = maybe_alarm {
								alarm = alarm.min(set_alarm);
							}
							dirty = true;
							branch
						} else {
							alarm = alarm.min(prepare_end);
							ServiceBranch::Preparing
						}
					} else {
						alarm = timeout;
						ServiceBranch::NoDeposit
					}
				}
				// If we didn't move into being decided, then check the timeout.
				if status.deciding.is_none() && now >= timeout && !status.in_queue {
					// Too long without being decided - end it.
					Self::ensure_no_alarm(&mut status);
					Self::deposit_event(Event::<T, I>::TimedOut { index, tally: status.tally });
					return (
						ReferendumInfo::TimedOut(
							now,
							Some(status.submission_deposit),
							status.decision_deposit,
						),
						true,
						ServiceBranch::TimedOut,
					);
				}
```

**File:** substrate/frame/referenda/src/lib.rs (L1224-1248)
```rust
					if now >= deciding.since.saturating_add(track.decision_period) {
						// Failed!
						Self::ensure_no_alarm(&mut status);
						Self::note_one_fewer_deciding(status.track);
						Self::deposit_event(Event::<T, I>::Rejected { index, tally: status.tally });
						return (
							ReferendumInfo::Rejected(
								now,
								Some(status.submission_deposit),
								status.decision_deposit,
							),
							true,
							ServiceBranch::Rejected,
						);
					}
					if deciding.confirming.is_some() {
						// Stop confirming
						dirty = true;
						deciding.confirming = None;
						Self::deposit_event(Event::<T, I>::ConfirmAborted { index });
						ServiceBranch::EndConfirming
					} else {
						ServiceBranch::ContinueNotConfirming
					}
				};
```

**File:** substrate/bin/node/runtime/src/lib.rs (L247-268)
```rust
/// Calls that can bypass the safe-mode pallet.
pub struct SafeModeWhitelistedCalls;
impl Contains<RuntimeCall> for SafeModeWhitelistedCalls {
	fn contains(call: &RuntimeCall) -> bool {
		match call {
			RuntimeCall::System(_) | RuntimeCall::SafeMode(_) | RuntimeCall::TxPause(_) => true,
			_ => false,
		}
	}
}

/// Calls that cannot be paused by the tx-pause pallet.
pub struct TxPauseWhitelistedCalls;
/// Whitelist `Balances::transfer_keep_alive`, all others are pauseable.
impl Contains<RuntimeCallNameOf<Runtime>> for TxPauseWhitelistedCalls {
	fn contains(full_name: &RuntimeCallNameOf<Runtime>) -> bool {
		match (full_name.0.as_slice(), full_name.1.as_slice()) {
			(b"Balances", b"transfer_keep_alive") => true,
			_ => false,
		}
	}
}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L355-374)
```rust
#[derive_impl(frame_system::config_preludes::SolochainDefaultConfig)]
impl frame_system::Config for Runtime {
	type BaseCallFilter = InsideBoth<SafeMode, TxPause>;
	type BlockWeights = RuntimeBlockWeights;
	type BlockLength = RuntimeBlockLength;
	type DbWeight = RocksDbWeight;
	type Nonce = Nonce;
	type Hash = Hash;
	type AccountId = AccountId;
	type Lookup = Indices;
	type Block = Block;
	type BlockHashCount = BlockHashCount;
	type Version = Version;
	type AccountData = pallet_balances::AccountData<Balance>;
	type SystemWeightInfo = frame_system::weights::SubstrateWeight<Runtime>;
	type SS58Prefix = ConstU16<42>;
	type MaxConsumers = ConstU32<16>;
	type MultiBlockMigrator = MultiBlockMigrations;
	type SingleBlockMigrations = Migrations;
}
```

**File:** substrate/frame/tx-pause/src/tests.rs (L139-148)
```rust
			Error::<Test>::Unpausable
		);
	});
}

#[test]
fn fails_to_pause_unpausable_call_when_other_call_is_paused() {
	new_test_ext().execute_with(|| {
		assert_ok!(call_transfer(1, 1).dispatch(RuntimeOrigin::signed(0)));

```

**File:** substrate/frame/safe-mode/src/lib.rs (L296-310)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Enter safe-mode permissionlessly for [`Config::EnterDuration`] blocks.
		///
		/// Reserves [`Config::EnterDepositAmount`] from the caller's account.
		/// Emits an [`Event::Entered`] event on success.
		/// Errors with [`Error::Entered`] if the safe-mode is already entered.
		/// Errors with [`Error::NotConfigured`] if the deposit amount is `None`.
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::enter())]
		pub fn enter(origin: OriginFor<T>) -> DispatchResult {
			let who = ensure_signed(origin)?;

			Self::do_enter(Some(who), T::EnterDuration::get()).map_err(Into::into)
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/governance/tracks.rs (L188-217)
```rust
	},
	pallet_referenda::Track {
		id: 16,
		info: pallet_referenda::TrackInfo {
			name: s("monetary_guard"),
			max_deciding: 3,
			// NOTE: Low for testing purposes.
			decision_deposit: 500 * UNITS,
			prepare_period: 20 * MINUTES,
			decision_period: 20 * MINUTES,
			confirm_period: 4 * MINUTES,
			min_enactment_period: 1 * MINUTES,
			min_approval: APP_MONETARY_GUARD,
			min_support: SUP_MONETARY_GUARD,
		},
	},
	pallet_referenda::Track {
		id: 20,
		info: pallet_referenda::TrackInfo {
			name: s("referendum_canceller"),
			max_deciding: 1_000,
			decision_deposit: 10 * GRAND,
			prepare_period: 8 * MINUTES,
			decision_period: 14 * MINUTES,
			confirm_period: 8 * MINUTES,
			min_enactment_period: 3 * MINUTES,
			min_approval: APP_REFERENDUM_CANCELLER,
			min_support: SUP_REFERENDUM_CANCELLER,
		},
	},
```
