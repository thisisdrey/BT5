### Title
Missing minimum-improvement threshold lets unprivileged submitters grief multi-block election verification with dust-sized score increments - ([File: substrate/frame/election-provider-multi-block/src/verifier/impls.rs])

### Summary
The external report's core broken invariant is: a "better than previous" check with **no minimum meaningful margin** lets an attacker repeatedly submit trivially-improved bids, each of which resets/consumes the time-locked verification window, denying other participants and degrading the outcome. In `pallet-election-provider-multi-block`, the verifier's `ensure_score_quality` accepts any solution that is merely `strict_better` than the currently queued score — there is no minimum-improvement threshold anymore (the previous `SolutionImprovementThreshold`/`BetterSignedThreshold` mechanism was intentionally removed, per `prdoc/stable2603/pr_10340.prdoc`). This mirrors the Gondi bug precisely: dust-sized improvements are sufficient to invalidate/replace a fully verified, multi-block-expensive solution.

### Finding Description
`ensure_score_quality` only requires the new score be `strict_better` than the queued one, with no floor on the delta: [1](#0-0) 

This check gates `AsynchronousVerifier::start`, which — if the score passes — puts the verifier into `Status::Ongoing` and begins a **multi-block** feasibility check that consumes weight page-by-page over several blocks: [2](#0-1) [3](#0-2) 

Because `strict_better` requires only a strictly-greater score (any positive delta, e.g. `minimal_stake + 1`), an unprivileged signed submitter can:
1. Submit an initial full-page solution and let it get fully verified/queued (consuming `Pages` blocks of verification weight).
2. Submit a second solution with a score improved by the smallest possible unit.
3. Because it passes `ensure_score_quality`, the signed pallet will trigger another full asynchronous multi-block verification pass for the new "leader," again consuming `Pages` blocks of weight and displacing/clearing the previously queued valid solution's backing data during the run.

The doc comment for `AsynchronousVerifier::start` even acknowledges that the `SolutionDataProvider` (the signed pallet) is expected to keep offering "a new candidate solution" and that `start` should be re-invoked — the design assumes a bounded, cooperative flow, not repeated dust-improvement submissions from many unprivileged signed accounts within the same round. The old defense against exactly this griefing pattern — `BetterSignedThreshold`/`SolutionImprovementThreshold`, requiring a minimum `Perbill` improvement before ejecting/restarting on a "better" score — has been fully removed from this pallet, as confirmed by the PRDoc: [4](#0-3) 

This is structurally identical to the Gondi `placeBid` bug: a percentage/threshold-less "greater than previous" comparison lets an attacker win/occupy the contested resource (here: verification cycles / the signed queue's finalization slot) using minimal increments, at the cost of the honest participants' time and weight budget, while consuming disproportionate on-chain verification capacity that is meant to be a scarce, bounded resource per round (`SignedValidation` phase length is fixed).

### Impact Explanation
Each fresh multi-block verification cycle re-consumes `T::Pages::get()` blocks worth of verification weight (`per_block_exec`/`do_per_block_exec`), which is the scarce resource intended to guarantee an election result is finalized before the round's fixed `SignedValidation` phase elapses. Because the improvement bar is effectively zero, a well-funded unprivileged submitter can keep resetting the "winning" claim with dust-sized score bumps, consuming the phase's available verification blocks on repeated re-verifications of near-identical solutions rather than allowing the phase to conclude cleanly with the best available (and cheaply-arrived-at) solution. In the worst case, this can push the round to fail to produce a finalized solution in time, forcing a fallback election path (potentially `EmergencyPhase`/onchain fallback), which directly degrades block production reliability of the staking election pipeline — the "public underpriced work that degrades block production" impact class.

### Likelihood Explanation
The action requires only a signed, unprivileged account with enough balance to pay the submission deposit for each of several `submit`-style calls within the same round's signed phase — no relayer, validator, governance, or leaked-key assumptions are needed. The only cost to the attacker is the (bounded) deposit and repeated calls to mine/submit a solution with a trivially incremented score, which is computationally far cheaper than mining a genuinely better solution. This is a strictly weaker bar than what existed before the threshold removal, so the likelihood is non-negligible in any live deployment of `pallet-election-provider-multi-block` with open signed submissions.

### Recommendation
Reintroduce a minimum-improvement floor (an equivalent of the removed `BetterSignedThreshold`/`SolutionImprovementThreshold`, or a fixed absolute `MinimumScoreDelta`) in `ensure_score_quality`, so that a new solution must exceed the currently queued score by at least a configured margin before it is allowed to trigger a full re-verification pass, not merely be `strict_better`. Alternatively, rate-limit or cap the number of times per round that a fresh async verification can be started for a given round to bound worst-case weight consumption regardless of how many marginal-improvement submissions arrive.

### Proof of Concept
1. Start a round; let account `A` submit `solution_1` (full pages) with score `S`. It fully verifies and is queued via `finalize_correct` (`Queued` event).
2. Immediately have account `B` submit `solution_2` with score `S + 1` (only `minimal_stake` incremented by one unit, or an analogous single-unit ordering-preserving tweak).
3. `ensure_score_quality` returns `Ok` because `S + 1` is `strict_better` than `S` (see `substrate/frame/election-provider-multi-block/src/verifier/impls.rs:824-827`).
4. `AsynchronousVerifier::start` transitions `Status` back to `Ongoing(msp)`, and the verifier re-runs the full `Pages`-length verification loop (`do_per_block_exec`), consuming another full multi-block verification cycle just to promote a solution that is functionally identical.
5. Repeat with accounts `C`, `D`, ... each submitting `score + 1` over `score`, indefinitely consuming the round's fixed verification-phase block budget with no meaningful improvement to the final election outcome, mirroring the reference report's DoS-via-dust-increment pattern.

### Citations

**File:** substrate/frame/election-provider-multi-block/src/verifier/impls.rs (L618-705)
```rust
impl<T: Config> Pallet<T> {
	fn do_per_block_exec() -> (Weight, Box<dyn Fn(&mut WeightMeter)>) {
		let Status::Ongoing(current_page) = Self::status_storage() else {
			let weight = T::DbWeight::get().reads(1);
			return (weight, Box::new(move |meter: &mut WeightMeter| meter.consume(weight)));
		};

		// before executing, we don't know which weight we will consume; return the max.
		let worst_case_weight = VerifierWeightsOf::<T>::verification_valid_non_terminal()
			.max(VerifierWeightsOf::<T>::verification_valid_terminal())
			.max(VerifierWeightsOf::<T>::verification_invalid_non_terminal(T::Pages::get()))
			.max(VerifierWeightsOf::<T>::verification_invalid_terminal());

		let execute = Box::new(move |meter: &mut WeightMeter| {
			let page_solution =
				<T::SolutionDataProvider as SolutionDataProvider>::get_page(current_page);
			let maybe_supports = Self::feasibility_check_page_inner(page_solution, current_page);

			sublog!(
				debug,
				"verifier",
				"verified page {} of a solution, outcome = {:?}",
				current_page,
				maybe_supports.as_ref().map(|s| s.len())
			);
			match maybe_supports {
				Ok(supports) => {
					Self::deposit_event(Event::<T>::Verified(current_page, supports.len() as u32));
					QueuedSolution::<T>::set_invalid_page(current_page, supports);

					if current_page > crate::Pallet::<T>::lsp() {
						// not last page, just move forward.
						StatusStorage::<T>::put(Status::Ongoing(
							current_page.defensive_saturating_sub(1),
						));
						meter.consume(VerifierWeightsOf::<T>::verification_valid_non_terminal())
					} else {
						// last page, finalize everything. Get the claimed score.
						let claimed_score = T::SolutionDataProvider::get_score();

						// in both cases of the following match, we are back to the nothing
						// state.
						StatusStorage::<T>::put(Status::Nothing);

						match Self::finalize_async_verification(claimed_score) {
							Ok(_) => {
								T::SolutionDataProvider::report_result(VerificationResult::Queued);
								meter.consume(VerifierWeightsOf::<T>::verification_valid_terminal())
							},
							Err(_) => {
								T::SolutionDataProvider::report_result(
									VerificationResult::Rejected,
								);
								// In case of any of the errors, kill the solution.
								QueuedSolution::<T>::clear_invalid_and_backings();
								meter
									.consume(VerifierWeightsOf::<T>::verification_invalid_terminal())
							},
						}
					}
				},
				Err(err) => {
					// the page solution was invalid.
					Self::deposit_event(Event::<T>::VerificationFailed(current_page, err));

					sublog!(warn, "verifier", "Clearing any ongoing unverified solution.");
					// Clear any ongoing solution that has not been verified, regardless of
					// the current state.
					QueuedSolution::<T>::clear_invalid_and_backings_unchecked();

					// we also mutate the status back to doing nothing.
					let was_ongoing = matches!(StatusStorage::<T>::get(), Status::Ongoing(_));
					StatusStorage::<T>::put(Status::Nothing);

					if was_ongoing {
						T::SolutionDataProvider::report_result(VerificationResult::Rejected);
					}
					let wasted_pages = T::Pages::get().saturating_sub(current_page);
					meter.consume(VerifierWeightsOf::<T>::verification_invalid_non_terminal(
						wasted_pages,
					))
				},
			}
		});

		(worst_case_weight, execute)
	}

```

**File:** substrate/frame/election-provider-multi-block/src/verifier/impls.rs (L820-834)
```rust
	/// Ensure that the given score is:
	///
	/// - better than the queued solution, if one exists.
	/// - greater than the minimum untrusted score.
	pub(crate) fn ensure_score_quality(score: ElectionScore) -> Result<(), FeasibilityError> {
		let is_improvement = <Self as Verifier>::queued_score()
			.map_or(true, |best_score| score.strict_better(best_score));
		ensure!(is_improvement, FeasibilityError::ScoreTooLow);

		let is_greater_than_min_untrusted =
			Self::minimum_score().map_or(true, |min_score| score.strict_better(min_score));
		ensure!(is_greater_than_min_untrusted, FeasibilityError::ScoreTooLow);

		Ok(())
	}
```

**File:** substrate/frame/election-provider-multi-block/src/verifier/impls.rs (L1005-1034)
```rust
impl<T: Config> AsynchronousVerifier for Pallet<T> {
	type SolutionDataProvider = T::SolutionDataProvider;

	fn status() -> Status {
		Pallet::<T>::status_storage()
	}

	fn start() -> Result<(), &'static str> {
		sublog!(debug, "verifier", "start signal received.");
		if let Status::Nothing = Self::status() {
			let claimed_score = Self::SolutionDataProvider::get_score();
			if Self::ensure_score_quality(claimed_score).is_err() {
				// don't do anything, report back that this solution was garbage.
				Self::deposit_event(Event::<T>::VerificationFailed(
					crate::Pallet::<T>::msp(),
					FeasibilityError::ScoreTooLow,
				));
				T::SolutionDataProvider::report_result(VerificationResult::Rejected);
				// Despite being an instant-reject, this was a successful `start` operation.
				Ok(())
			} else {
				// This solution is good enough to win, we start verifying it in the next block.
				StatusStorage::<T>::put(Status::Ongoing(crate::Pallet::<T>::msp()));
				Ok(())
			}
		} else {
			sublog!(warn, "verifier", "start signal received while busy. This will be ignored.");
			Err("verification ongoing")
		}
	}
```

**File:** prdoc/stable2603/pr_10340.prdoc (L1-14)
```text
title: Remove "SolutionImprovementThreshold" logic
doc:
- audience: Runtime Dev
  description: |-
    The threshold mechanism used by the `election-provider-multi-block` verifier pallet is no longer relevant. There are no queued solutions to compare during the initial verification. Solutions are subsequently processed in order of decreasing score, with the first verified solution being selected, while any remaining solutions are not verified.

crates:
- name: asset-hub-westend-runtime
  bump: major
- name: pallet-election-provider-multi-block
  bump: major
- name: pallet-election-provider-multi-phase
  bump: major
- name: sp-npos-elections
```
