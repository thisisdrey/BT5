### Title
UMP queue capacity check race in elastic-scaling: sibling candidates of the same para bypass `max_upward_queue_count` - (File: `polkadot/runtime/parachains/src/inclusion/mod.rs`)

### Summary
The external report's core defect is a **check-then-commit gap**: a per-request admission check (`archivers.size >= max`) is evaluated against the *currently committed* state while several concurrent requests are queued in the same batch, then **all** queued requests are unconditionally appended to the committed set once the batch is flushed — so the limit is enforced against a stale count and can be exceeded by any number of requests admitted in the same window.

The same shape of bug exists in the relay chain's UMP (Upward Message Passing) admission logic for elastic-scaling parachains (multiple cores assigned to one `ParaId` in the same relay block).

### Finding Description
`Inclusion::check_upward_messages` enforces the UMP queue bound by reading the *currently enqueued* footprint and adding only the messages of the single candidate being checked: [1](#0-0) 

`relay_dispatch_queue_size` is a live read of `T::MessageQueue::footprint`, which is **not** updated by this check — it is only updated later, when the messages are actually enqueued in `receive_upward_messages`/`receive_bounded_upward_messages`: [2](#0-1) 

Enqueuing (the "commit" step) only happens inside `enact_candidate`, which runs when a candidate becomes *available* (via bitfields), not when it is *backed*: [3](#0-2) 

The admission check itself is invoked at backing time, from `CandidateCheckContext::verify_backed_candidate` → `check_validation_outputs` → `check_upward_messages`, driven from `process_candidates`, which iterates **all cores assigned to a para in the same relay block** (elastic scaling) in a plain loop, without ever mutating the UMP footprint between iterations: [4](#0-3) [5](#0-4) 

Because a para can have several chained candidates backed on several cores in the same block (elastic scaling is explicitly tested with multiple candidates/cores for one `ParaId`): [6](#0-5) 

every sibling candidate's `check_upward_messages` call reads the **same, unmodified** `para_queue_count` baseline (since none of the sibling candidates has been enacted/enqueued yet), and each checks only its own `additional_msgs` against `config.max_upward_queue_count`. None of the checks account for the UMP messages that the *other* sibling candidates from the same block will also add. All of these candidates are later admitted to `PendingAvailability` unconditionally, and once each becomes available, `enact_candidate` calls the now-documented-as-"infallible" `receive_upward_messages`, which enqueues its messages into the same `AggregateMessageOrigin::Ump(UmpQueueId::Para(para))` queue with no further bound check: [7](#0-6) 

This is structurally identical to the Shardus bug: the acceptance check reads a snapshot of committed state (`archivers.size` / `para_queue_count`) instead of the state that will exist after all requests admitted in the same batch are committed, and the eventual commit step is unconditional.

### Impact Explanation
If the sum of upward messages from multiple sibling candidates of the same elastic-scaling `ParaId`, each individually within `max_upward_message_num_per_candidate` and each individually passing the stale `max_upward_queue_count` check, exceeds the configured queue-count/size bound once all are enacted, the relay chain's UMP queue for that para grows beyond its intended cap. `max_upward_queue_count`/`max_upward_queue_size` exist specifically to bound the PoV/weight cost of message-queue servicing and to protect against queue-based resource exhaustion (`config.max_upward_queue_count`, `config.max_upward_queue_size` in `HostConfiguration`). Bypassing this bound is exactly the "public underpriced work that degrades block production" class of impact: the relay chain would need to service/store more UMP messages than its configuration was sized for, which was the entire purpose of the limit that this check is supposed to enforce.

### Likelihood Explanation
Exploiting this requires only a normal, permissionless parachain slot with elastic scaling (multiple cores) and standard validator backing of its own candidates in the same relay block — it does not require any relay-chain validator, collator, or governance participant to act maliciously; it only requires the parachain's own (potentially unprivileged/buggy) runtime to emit near-maximum UMP messages from multiple cores' worth of candidates concurrently, which the check does not defend against because it evaluates each backed candidate in isolation against a shared, unadvanced baseline. This is a legitimate, code-provable gap in the admission logic, though it depends on the elastic-scaling multi-core scheduling path being active.

### Recommendation
`check_upward_messages`/`process_candidates` must account for UMP messages committed by *other candidates processed in the same batch* for the same para before admitting the next sibling candidate — either by tracking an in-memory running total per `ParaId` across the `process_candidates` loop (mirroring the shardus fix of checking `archivers.size + joinRequests.size`), or by moving the queue-capacity check to occur transactionally against a per-block accumulator that is updated after each candidate is provisionally accepted, rather than only being updated at enactment time.

### Proof of Concept
Conceptual reproduction (would need running node/test harness, not verifiable purely from static review):
1. Register a parachain with elastic scaling enabled and multiple cores assigned to the same `ParaId` in a single relay block (as in `include_backed_candidates_elastic_scaling`, referenced above).
2. Configure/observe `max_upward_queue_count = N` and let the para's current queue footprint sit close to `N` (e.g., `N - k`).
3. Have the parachain produce `m` chained candidates for the same block, each carrying close to `max_upward_message_num_per_candidate` UMP messages, such that any single candidate's check passes (`para_queue_count + additional_msgs <= N`), but the sum across all `m` candidates exceeds `N - (N-k) = k`.
4. Submit all `m` backed candidates together; `process_candidates` iterates them in the same loop, each calling `check_upward_messages` against the same unmodified footprint, and all pass.
5. As each candidate becomes available across subsequent blocks, `enact_candidate`/`receive_upward_messages` unconditionally enqueues its messages, driving the UMP queue for that para above the configured `max_upward_queue_count`.

### Citations

**File:** polkadot/runtime/parachains/src/inclusion/mod.rs (L648-731)
```rust
		for (para_id, para_candidates) in candidates {
			let mut latest_head_data = match Self::para_latest_head_data(para_id) {
				None => {
					defensive!("Latest included head data for paraid {:?} is None", para_id);
					continue;
				},
				Some(latest_head_data) => latest_head_data,
			};

			for (candidate, core) in para_candidates.iter() {
				let candidate_hash = candidate.candidate().hash();

				// The previous context is None, as it's already checked during candidate
				// sanitization.
				let check_ctx = CandidateCheckContext::<T>::new(None);
				let relay_parent_number = check_ctx
					.verify_backed_candidate(candidate.candidate(), latest_head_data.clone())?;

				let scheduling_parent = candidate.descriptor().scheduling_parent();

				let (_, scheduling_parent_number) = allowed_scheduling_parents
					.acquire_info(scheduling_parent)
					.ok_or(Error::<T>::DisallowedSchedulingParent)?;

				// The candidate based upon scheduling parent `N` should be backed by a
				// group assigned to core at block `N + 1`. Thus,
				// `scheduling_parent_number + 1` will always land in the current
				// session.
				let group_idx = scheduler::Pallet::<T>::group_assigned_to_core(
					*core,
					scheduling_parent_number + One::one(),
				)
				.ok_or_else(|| {
					log::warn!(
						target: LOG_TARGET,
						"Failed to compute group index for candidate {:?}",
						candidate_hash
					);
					Error::<T>::InvalidAssignment
				})?;
				let group_vals =
					group_validators(group_idx).ok_or_else(|| Error::<T>::InvalidGroupIndex)?;

				// Check backing vote count and validity.
				let (backers, backer_idx_and_attestation) =
					Self::check_backing_votes(candidate, &validators, group_vals)?;

				// Found a valid candidate.
				latest_head_data = candidate.candidate().commitments.head_data.clone();
				candidate_receipt_with_backing_validator_indices
					.push((candidate.receipt(), backer_idx_and_attestation));

				// Update storage now
				PendingAvailability::<T>::mutate(&para_id, |pending_availability| {
					let new_candidate = CandidatePendingAvailability {
						core: *core,
						hash: candidate_hash,
						descriptor: candidate.candidate().descriptor.clone(),
						commitments: candidate.candidate().commitments.clone(),
						// initialize all availability votes to 0.
						availability_votes: bitvec::bitvec![u8, BitOrderLsb0; 0; validators.len()],
						relay_parent_number,
						backers: backers.to_bitvec(),
						backed_in_number: now,
						backing_group: group_idx,
					};

					if let Some(pending_availability) = pending_availability {
						pending_availability.push_back(new_candidate);
					} else {
						*pending_availability =
							Some([new_candidate].into_iter().collect::<VecDeque<_>>())
					}
				});

				// Deposit backed event.
				Self::deposit_event(Event::<T>::CandidateBacked(
					candidate.candidate().to_plain(),
					candidate.candidate().commitments.head_data.clone(),
					*core,
					group_idx,
				));
			}
		}
```

**File:** polkadot/runtime/parachains/src/inclusion/mod.rs (L888-896)
```rust
		// enact the messaging facet of the candidate.
		dmp::Pallet::<T>::prune_dmq(
			receipt.descriptor.para_id(),
			commitments.processed_downward_messages,
		);
		Self::receive_upward_messages(
			receipt.descriptor.para_id(),
			commitments.upward_messages.as_slice(),
		);
```

**File:** polkadot/runtime/parachains/src/inclusion/mod.rs (L939-954)
```rust
		let additional_msgs = upward_messages.len() as u32;
		if additional_msgs > config.max_upward_message_num_per_candidate {
			return Err(UmpAcceptanceCheckErr::MoreMessagesThanPermitted {
				sent: additional_msgs,
				permitted: config.max_upward_message_num_per_candidate,
			});
		}

		let (para_queue_count, mut para_queue_size) = Self::relay_dispatch_queue_size(para);

		if para_queue_count.saturating_add(additional_msgs) > config.max_upward_queue_count {
			return Err(UmpAcceptanceCheckErr::CapacityExceeded {
				count: para_queue_count.saturating_add(additional_msgs).into(),
				limit: config.max_upward_queue_count.into(),
			});
		}
```

**File:** polkadot/runtime/parachains/src/inclusion/mod.rs (L980-1013)
```rust
	/// Enqueues `upward_messages` from a `para`'s accepted candidate block.
	///
	/// This function is infallible since the candidate was already accepted and we therefore need
	/// to deal with the messages as given. Messages that are too long will be ignored since such
	/// candidates should have already been rejected in [`Self::check_upward_messages`].
	pub(crate) fn receive_upward_messages(para: ParaId, upward_messages: &[Vec<u8>]) {
		let bounded = skip_ump_signals(upward_messages.iter())
			.filter_map(|d| {
				BoundedSlice::try_from(&d[..])
					.inspect_err(|_| {
						defensive!("Accepted candidate contains too long msg, len=", d.len());
					})
					.ok()
			})
			.collect();
		Self::receive_bounded_upward_messages(para, bounded)
	}

	/// Enqueues storage-bounded `upward_messages` from a `para`'s accepted candidate block.
	pub(crate) fn receive_bounded_upward_messages(
		para: ParaId,
		messages: Vec<BoundedSlice<'_, u8, MaxUmpMessageLenOf<T>>>,
	) {
		let count = messages.len() as u32;
		if count == 0 {
			return;
		}

		T::MessageQueue::enqueue_messages(
			messages.into_iter(),
			AggregateMessageOrigin::Ump(UmpQueueId::Para(para)),
		);
		Self::deposit_event(Event::UpwardMessagesReceived { from: para, count });
	}
```

**File:** polkadot/runtime/parachains/src/inclusion/mod.rs (L1236-1318)
```rust
	pub(crate) fn verify_backed_candidate(
		&self,
		backed_candidate_receipt: &CommittedCandidateReceipt<<T as frame_system::Config>::Hash>,
		parent_head_data: HeadData,
	) -> Result<BlockNumberFor<T>, Error<T>> {
		let para_id = backed_candidate_receipt.descriptor.para_id();
		let relay_parent = backed_candidate_receipt.descriptor.relay_parent();

		// For V1: session_index() returns None, falls back to current session.
		// For V2: session_index() returns the embedded value, which
		//   check_descriptor_version_and_signals already verified == current session.
		// For V3: session_index() returns the embedded value, which may differ from
		//   current session (cross-session relay parents).
		let session_index = backed_candidate_receipt
			.descriptor
			.session_index()
			.unwrap_or_else(|| shared::CurrentSessionIndex::<T>::get());

		// Check that the relay-parent is one of the allowed relay-parents.
		let (state_root, relay_parent_number) = {
			match shared::Pallet::<T>::get_relay_parent_info(session_index, relay_parent) {
				None => return Err(Error::<T>::DisallowedRelayParent),
				Some(info) => (info.state_root, info.number),
			}
		};

		// Candidate's relay parent cannot move backwards.
		if let Some(prev_context) = self.prev_context {
			if relay_parent_number < prev_context {
				return Err(Error::<T>::DisallowedRelayParent);
			}
		}

		{
			let persisted_validation_data = make_persisted_validation_data_with_parent::<T>(
				relay_parent_number,
				state_root,
				parent_head_data,
			);

			let expected = persisted_validation_data.hash();

			ensure!(
				expected == backed_candidate_receipt.descriptor.persisted_validation_data_hash(),
				Error::<T>::ValidationDataHashMismatch,
			);
		}

		let validation_code_hash = paras::CurrentCodeHash::<T>::get(para_id)
			// A candidate for a parachain without current validation code is not scheduled.
			.ok_or_else(|| Error::<T>::UnscheduledCandidate)?;
		ensure!(
			backed_candidate_receipt.descriptor.validation_code_hash() == validation_code_hash,
			Error::<T>::InvalidValidationCodeHash,
		);

		ensure!(
			backed_candidate_receipt.descriptor.para_head() ==
				backed_candidate_receipt.commitments.head_data.hash(),
			Error::<T>::ParaHeadMismatch,
		);

		if let Err(err) = self.check_validation_outputs(
			para_id,
			relay_parent_number,
			&backed_candidate_receipt.commitments.head_data,
			&backed_candidate_receipt.commitments.new_validation_code,
			backed_candidate_receipt.commitments.processed_downward_messages,
			&backed_candidate_receipt.commitments.upward_messages,
			BlockNumberFor::<T>::from(backed_candidate_receipt.commitments.hrmp_watermark),
			&backed_candidate_receipt.commitments.horizontal_messages,
		) {
			log::debug!(
				target: LOG_TARGET,
				"Validation outputs checking during inclusion of a candidate {:?} for parachain `{}` failed, error: {:?}",
				backed_candidate_receipt.hash(),
				u32::from(para_id),
				err
			);
			Err(err.strip_into_dispatch_err::<T>())?;
		};
		Ok(relay_parent_number)
	}
```

**File:** polkadot/runtime/parachains/src/paras_inherent/tests.rs (L252-258)
```rust
	fn include_backed_candidates_elastic_scaling(#[case] v2_descriptor: bool) {
		// ParaId 0 has one pending candidate on core 0.
		// ParaId 1 has one pending candidate on core 1.
		// ParaId 2 has three pending candidates on cores 2, 3 and 4.
		// All of them are being made available in this block. Propose 5 more candidates (one for
		// each core) and check that they're successfully backed and the old ones enacted.
		let config = default_config();
```
