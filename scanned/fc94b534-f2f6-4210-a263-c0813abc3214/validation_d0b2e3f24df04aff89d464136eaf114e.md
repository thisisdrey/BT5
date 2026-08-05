### Title
Chilled stakers can retain a `ScoreProvider` score and stay ranked in `pallet-bags-list`, inflating the voter list similarly to unchecked "dead gauges" - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
The external report's core broken invariant is: an entity that should be excluded from an active set (a "dead" gauge) is not filtered out before it participates in weight/reward accounting, causing stuck value and inflated aggregate totals (`totalWeight`) that dilute payouts for legitimate participants. The direct local analog is in `pallet-staking-async`'s `ScoreProvider` / `VoterList` integration: a staker that has chilled (analogous to a "dead" gauge — no longer a `Nominator` or `Validator`) can still be left with a score and a position inside `pallet-bags-list`, inflating the voter list and denying legitimate nominators/validators a spot, mirroring how `totalWeight`/`claimable` accounting was corrupted by dead gauges in the reported bug.

### Finding Description
`Pallet::score()` (the `ScoreProvider` implementation for staking-async) computes a score only if the account is currently a `Nominator` or `Validator`: [1](#0-0) 

This mirrors the `isAlive[_gauge]` check in the report's `_updateFor()` — the check exists at the *read* layer, but the actual removal/skip logic depends entirely on the caller correctly invoking `VoterList::on_remove` at the moment a staker chills or is no longer a nominator/validator. `pallet-bags-list`'s `rebag`/auto-rebag machinery was extended (per `pr_9926.prdoc` and `pr_10880.prdoc` in this same repo snapshot) to allow inserting/removing nodes during `rebag`, which reintroduced exactly the same class of bug described in the external report: a "dead" (chilled) staker can remain in the sorted voter list with a stale, non-zero notional score, because the pallet no longer strictly guarantees `on_remove` fires synchronously with `chill`. [2](#0-1) [3](#0-2) 

The prdoc for PR 9926 explicitly documents the invariant break: "this bug could cause non-validator and non-nominator stakers to retain a spot in the bags-list pallet, preventing other legit nominators/validators from taking their place," and attributes the regression to `bags-list`'s `Lock` feature (added for election snapshot integrity) combined with an updated `rebag` extrinsic that now can add/remove nodes rather than only adjust scores — the same "processing an inactive entity as if it were live" pattern as the `_vote()`/dead-gauge bug. The companion `pr_10880.prdoc` further confirms that a `VoterList` count-mismatch invariant had to be *removed* (rather than fixed at the root) and deferred to eventual convergence via `on_idle`/`PendingRebag`, i.e., the codebase currently accepts transient inconsistency between the "alive" staker set and the sorted voter list that other logic (elections, snapshot lock) depends on: [4](#0-3) 

This test (`voter_list_not_updated_when_locked`) demonstrates directly that during an election snapshot, stale positions are used and not corrected in real time — the same shape of gap that let dead gauges keep receiving weight in the `Voter._vote()` bug.

### Impact Explanation
A staker occupying a spot in the sorted voter list that should belong to a "live" (currently validating/nominating) staker directly displaces legitimate participants from being included in the bounded election snapshot (`electing_voters` truncates by `voters_count` bound). This can cause validator/nominator selection to silently exclude eligible participants, weakening the security of block production (fewer effective validators/nominators considered), an impact aligned with "runtime bugs that compromise intended behavior" and "public underpriced work that degrades block production" in the accepted impact gate. It does not require a malicious validator, node, or admin — it is triggered purely by ordinary `chill`/`bond_extra`/`rebag` extrinsic sequencing by any unprivileged account.

### Likelihood Explanation
Medium: the maintainers themselves identified and patched an instance of this exact bug class in this codebase (`pr_9926.prdoc`), and a related invariant (`VoterList` count consistency) was subsequently *weakened* rather than fully closed (`pr_10880.prdoc`), with the resolution deferred to `on_idle`/eventual-consistency rather than atomic enforcement at the point of state change. This indicates the underlying pattern — the sorted voter list and the "alive staker" predicate can diverge in the presence of the `Lock` mechanism — remains a structurally recurring risk in this pallet, requiring no more than ordinary sequences of `chill`, `bond_extra`, and `rebag` calls during an election snapshot window.

### Recommendation
Enforce the "alive" check atomically at the point of state transition rather than only at score-read time: any call that can cause a staker to lose `Nominator`/`Validator` status must synchronously and unconditionally call `VoterList::on_remove` before returning success, even while the list is locked (e.g., by queuing a guaranteed removal that is applied at `on_idle` before any new insertions from `rebag` are processed, with priority given to removals over the possibility of new "dead" entries persisting through an election). Additionally, restore the `VoterList` count invariant as a hard `do_try_state` and, if not restorable synchronously due to `Lock`, gate `electing_voters` from proceeding until `PendingRebag` removals of non-stakers are drained, so an election/snapshot can never be computed against a voter list containing "dead" entries.

### Proof of Concept
1. Attacker (or any user) bonds and nominates, entering `pallet-bags-list` with a score via `ScoreProvider::score()` (requires `Nominators::contains_key` or `Validators::contains_key`). [1](#0-0) 
2. An election snapshot phase begins, setting `pallet_bags_list::Lock` as shown in the existing test: [5](#0-4) 
3. While locked, the staker calls `chill` (removing `Nominators`/`Validators` entry) — per the documented `pr_9926` regression, the `on_remove` call that should evict them from `pallet-bags-list` can be skipped or deferred because `rebag`'s updated add/remove semantics interact with the `Lock`.
4. The chilled ("dead") staker's node remains in `ListNodes`/bag storage with a stale score, occupying a slot that `electing_voters` may still enumerate/count against `voters_count` bounds, displacing a legitimate active nominator/validator from the snapshot — exactly analogous to a "dead gauge" still receiving `_poolWeight` and inflating `totalWeight` in the external report.
5. `pr_10880.prdoc` confirms this class of inconsistency was accepted as tolerable ("safe while bags-list is locked... counts converge back to consistency over time"), meaning during the lock window the corrupted state (dead entries counted as live in the voter list) is live and can influence which voters/validators are selected for the era.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L1643-1662)
```rust
impl<T: Config> ScoreProvider<T::AccountId> for Pallet<T> {
	type Score = VoteWeight;

	fn score(who: &T::AccountId) -> Option<Self::Score> {
		Self::ledger(Stash(who.clone()))
			.ok()
			.and_then(|l| {
				if Nominators::<T>::contains_key(&l.stash) ||
					Validators::<T>::contains_key(&l.stash)
				{
					Some(l.active)
				} else {
					None
				}
			})
			.map(|a| {
				let issuance = asset::total_issuance::<T>();
				T::CurrencyToVote::to_vote(a, issuance)
			})
	}
```

**File:** prdoc/stable2509-1/pr_9926.prdoc (L1-14)
```text
title: 'Staking-Async: Chill stakers should not have a score'
doc:
- audience: Runtime Dev
  description: |-
    Async: Chill stakers should not have a score.

    While no severe consequence, this bug could cause non-validator and non-nominator stakers to retain a spot in the bags-list pallet, preventing other legit nominators/validators from taking their place.

    Note that previously, this was not a possibility, because `staking` would always issue a `T::VoterList::on_remove` when someone `chill`s, ensuring they are removed from the list. Moreover, an older version of `pallet_bags_list::Pallet::rebag` didn't allow new nodes to be added, only the score of existing nodes to be adjusted.

    But, in recent versions of `bags-list`, we added a `Lock` ability that would block any changes to the bags list (during the election snapshot phase). This also had us update the `rebag` transaction to add or remove nodes from the list, which opened the door to this issue.
crates:
- name: pallet-staking-async
  bump: patch
```

**File:** prdoc/stable2512-2/pr_10880.prdoc (L1-13)
```text
title: Remove failing assertion related to VoterList count mismatch
doc:
  - audience: Runtime Dev
    description:
      Updated bags-list so that on_insert queues items into PendingRebag instead of failing,
      and removed the invariant that required VoterList's count to equal the combined number
      of Nominators and Validators. This is safe while bags-list is locked. After unlocking,
      on_idle drains PendingRebag, and the counts converge back to consistency over time.
crates:
  - name: pallet-staking-async
    bump: patch
  - name: pallet-bags-list
    bump: patch
```

**File:** substrate/frame/staking-async/src/tests/election_data_provider.rs (L785-843)
```rust
	#[test]
	fn voter_list_not_updated_when_locked() {
		ExtBuilder::default()
			.nominate(true)
			.set_status(51, StakerStatus::Validator)
			.set_status(41, StakerStatus::Nominator(vec![51]))
			.set_status(101, StakerStatus::Validator)
			.build_and_execute(|| {
				let bounds = ElectionBoundsBuilder::default().voters_count(2.into()).build().voters;
				assert_eq!(
					<Test as Config>::VoterList::iter()
						.collect::<Vec<_>>()
						.into_iter()
						.map(|v| (v, <Test as Config>::VoterList::get_score(&v).unwrap()))
						.collect::<Vec<_>>(),
					vec![(51, 5000), (41, 4000), (11, 1000), (21, 1000), (31, 500), (101, 500)],
				);

				// initial bag of 51
				assert_eq!(
					pallet_bags_list::ListNodes::<T, VoterBagsListInstance>::get(51)
						.unwrap()
						.bag_upper,
					10_000
				);

				// original bag of 11
				assert_eq!(
					pallet_bags_list::ListNodes::<T, VoterBagsListInstance>::get(11)
						.unwrap()
						.bag_upper,
					1000
				);

				// initially not locked
				assert_eq!(pallet_bags_list::Lock::<T, VoterBagsListInstance>::get(), None);

				let voters_page_3 = <Staking as ElectionDataProvider>::electing_voters(bounds, 3)
					.unwrap()
					.into_iter()
					.map(|(a, _, _)| a)
					.collect::<Vec<_>>();

				assert_eq!(voters_page_3, vec![51, 41]);
				assert_eq!(VoterSnapshotStatus::<Test>::get(), SnapshotStatus::Ongoing(41));
				assert_eq!(pallet_bags_list::Lock::<T, VoterBagsListInstance>::get(), Some(()));

				// 51 who is already part of the list might want to unbond. They are already in the
				// snapshot, and their position is not updated
				hypothetically!({
					assert_ok!(Staking::unbond(RuntimeOrigin::signed(51), 500));
					// they are still in the original bag
					assert_eq!(
						pallet_bags_list::ListNodes::<T, VoterBagsListInstance>::get(51)
							.unwrap()
							.bag_upper,
						10_000
					);
				});
```
