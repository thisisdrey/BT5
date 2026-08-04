This is a genuine local analog: `try_vote` in `substrate/frame/conviction-voting/src/lib.rs` calls the `on_before_vote` hook *before* validating that `poll_index` refers to an ongoing poll.

### Title
`on_before_vote` hook side effects persist for non-existent/non-ongoing polls before existence is validated - (File: substrate/frame/conviction-voting/src/lib.rs)

### Summary
`Pallet::try_vote` invokes `T::VotingHooks::on_before_vote(who, poll_index, vote)?` before calling `T::Polls::try_access_poll(poll_index, ...)`, which is the point where poll existence/ongoing-status is actually checked [1](#0-0) . Any hook implementation that mutates persistent state (fee charge, counter increment, reward-eligibility bookkeeping, etc.) will have that state committed even though the subsequent `try_access_poll` call fails with `Error::<T, I>::NotOngoing` for a bad/expired/non-existent `poll_index`, causing the whole extrinsic to revert its own storage changes but not the hook's, unless the hook itself is written defensively. This mirrors the reported Gov contract bug: user-controlled invalid identifiers cause side effects (there: lock extension; here: whatever `on_before_vote` does) despite the "real" vote never being recorded.

### Finding Description
In `try_vote`:
```rust
fn try_vote(
    who: &T::AccountId,
    poll_index: PollIndexOf<T, I>,
    vote: AccountVote<BalanceOf<T, I>>,
) -> DispatchResult {
    ensure!(
        vote.balance() <= T::Currency::total_balance(who),
        Error::<T, I>::InsufficientFunds
    );
    // Call on_vote hook
    T::VotingHooks::on_before_vote(who, poll_index, vote)?;

    T::Polls::try_access_poll(poll_index, |poll_status| {
        let (tally, class) = poll_status.ensure_ongoing().ok_or(Error::<T, I>::NotOngoing)?;
        ...
    })
}
``` [1](#0-0) 

The `poll_index` supplied by the caller is never checked for validity/ongoing status until `try_access_poll` executes the closure and calls `ensure_ongoing()` [2](#0-1) . `on_before_vote` runs unconditionally first, on an unvalidated index. The trait doc itself flags the danger:
```
/// - If `on_vote` succeeds but the voting operation fails later, any storage
///   modifications made by `on_vote` will still persist
``` [3](#0-2) 

This is exactly the pattern the external report describes: a public, unprivileged, user-facing `vote` extrinsic accepts an unchecked index; state that is *supposed* to be conditioned on a legitimate/existing poll is instead updated regardless, because the "does this poll exist" check happens too late relative to the side-effecting call. Whether this becomes an exploitable vulnerability depends entirely on what a concrete `VotingHooks` implementation does in `on_before_vote` — the default no-op `()` implementation is safe [4](#0-3) , but any runtime that wires in a non-trivial hook (e.g., to record participation for rewards, charge fees, or move funds/points) inherits this bug automatically, with no additional code changes needed on their part, because the ordering flaw is baked into `pallet-conviction-voting` itself.

### Impact Explanation
If a runtime's `VotingHooks` implementation performs balance-affecting or accounting-affecting actions (fee deduction, participation-reward bookkeeping, staking/",lock" side effects, etc.), an attacker can call `vote` with an arbitrary/garbage/expired `poll_index` repeatedly and force those side effects to be committed on every call even though the vote itself always reverts with `NotOngoing`. This can be used to grief reward/points accounting, drain fee-exempt participation bonuses, or otherwise desynchronize state that is meant to only advance on a successful vote — matching the "public underpriced work" / "state advances before settlement succeeds" impact class called out in the pivots.

### Likelihood Explanation
This requires no privileged actor — it's directly reachable through the public, unprivileged `vote` extrinsic with a fully attacker-controlled `poll_index`, requiring only a signed account with a nonzero balance to satisfy the earlier `InsufficientFunds` check. The severity is gated by what concrete `VotingHooks` implementation a downstream runtime plugs in; because `pallet-conviction-voting` is a generic, widely reused pallet across Polkadot SDK-based chains, any such non-trivial hook implementation is automatically exposed.

### Recommendation
Reorder `try_vote` so that `T::Polls::try_access_poll` (and its `ensure_ongoing()` check) is validated first, and only call `T::VotingHooks::on_before_vote` once the poll's existence and ongoing status are confirmed — analogous to the reported fix of gating the state-changing effect on `proposalId <= proposalCounter` before performing it.

### Proof of Concept
1. Deploy/configure a runtime using `pallet-conviction-voting` with a custom `VotingHooks` implementation whose `on_before_vote` performs a state mutation with real consequences (e.g. increments a per-account participation/reward counter, or reserves/charges a small fee) unconditionally on success.
2. From an unprivileged signed account with sufficient free balance, call `vote(origin, poll_index, vote)` using a `poll_index` that does not correspond to any ongoing poll (e.g., `u32::MAX` or an already-completed referendum index).
3. Observe that `on_before_vote` executes and its storage mutation is committed, then `try_access_poll` returns `Error::NotOngoing`, causing the extrinsic to fail overall — but the hook's side effect (unlike the rest of the transaction) is not rolled back if the hook writes to storage outside the transactional context guaranteed by the pallet's own `try_mutate`.
4. Repeating this call is unlimited (bounded only by transaction fees), letting the attacker accumulate the hook's side effect without ever casting a valid vote.

### Citations

**File:** substrate/frame/conviction-voting/src/lib.rs (L425-441)
```rust
impl<T: Config<I>, I: 'static> Pallet<T, I> {
	/// Actually enact a vote, if legit.
	fn try_vote(
		who: &T::AccountId,
		poll_index: PollIndexOf<T, I>,
		vote: AccountVote<BalanceOf<T, I>>,
	) -> DispatchResult {
		ensure!(
			vote.balance() <= T::Currency::total_balance(who),
			Error::<T, I>::InsufficientFunds
		);
		// Call on_vote hook
		T::VotingHooks::on_before_vote(who, poll_index, vote)?;

		T::Polls::try_access_poll(poll_index, |poll_status| {
			let (tally, class) = poll_status.ensure_ongoing().ok_or(Error::<T, I>::NotOngoing)?;
			VotingFor::<T, I>::try_mutate(who, &class, |voting| {
```

**File:** substrate/frame/conviction-voting/src/traits.rs (L51-58)
```rust
/// These hooks are called BEFORE the actual vote is recorded in storage. This means:
/// - If `on_vote` returns an error, the entire voting operation will be reverted
/// - If `on_vote` succeeds but the voting operation fails later, any storage modifications made by
///   `on_vote` will still persist
///
/// # Hook Methods
/// - `on_vote`: Called before a vote is recorded. Returns `Err` to prevent the vote from being
///   recorded. Storage modifications made by this hook will persist even if the vote fails later.
```

**File:** substrate/frame/conviction-voting/src/traits.rs (L105-108)
```rust
impl<A, I, B> VotingHooks<A, I, B> for () {
	fn on_before_vote(_who: &A, _ref_index: I, _vote: AccountVote<B>) -> DispatchResult {
		Ok(())
	}
```
