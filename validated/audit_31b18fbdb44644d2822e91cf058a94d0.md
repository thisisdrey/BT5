### Title
Borda-count "prime member" weight is skewed by unvalidated vote-array positions in `do_phragmen` - (File: `substrate/frame/elections-phragmen/src/lib.rs`)

### Summary
The reported pattern is: a caller supplies an array with a mix of valid and invalid targets; the pallet computes a per-item weight using the *position/size of the raw input array* instead of only the *valid, winning subset*, so legitimate targets receive a diluted/incorrect weight without the call ever reverting. `pallet-elections-phragmen`'s `do_phragmen` reproduces exactly this class of bug in its Borda-count prime-member selection: the vote multiplier is derived from each candidate's raw position inside the voter-supplied `votes: Vec<T::AccountId>`, and that array is never validated or filtered before the multiplier is computed.

### Finding Description
`Pallet::vote` (`substrate/frame/elections-phragmen/src/lib.rs:371-423`) lets any signed account submit an arbitrary `votes: Vec<T::AccountId>` up to `MaxVotesPerVoter`. Per the pallet's own docs, "a voter might vote for a future candidate" — i.e. `vote()` does **not** require the entries to currently be members/runners-up/candidates; the only checks are length bounds (`MaximumVotesExceeded`, `NoVotes`, `TooManyVotes`) and balance (`LowBalance`).

Later, `do_phragmen` (`substrate/frame/elections-phragmen/src/lib.rs:1056-1071`) picks the `prime` member with a Borda count:

```rust
let mut prime_votes = new_members_sorted_by_id
    .iter()
    .map(|c| (&c.0, BalanceOf::<T>::zero()))
    .collect::<Vec<_>>();
for (_, stake, votes) in voters_and_stakes.into_iter() {
    for (vote_multiplier, who) in
        votes.iter().enumerate().map(|(vote_position, who)| {
            ((T::MaxVotesPerVoter::get() as usize - vote_position) as u32, who)
        })
    {
        if let Ok(i) = prime_votes.binary_search_by_key(&who, |k| k.0) {
            prime_votes[i].1 = prime_votes[i].1.saturating_add(stake.saturating_mul(vote_multiplier.into()));
        }
    }
}
let prime = prime_votes.into_iter().max_by_key(|x| x.1).map(|x| x.0.clone());
```

`vote_multiplier` is `MaxVotesPerVoter - vote_position`, computed from the *raw index in the voter's array*. Entries that turn out not to be winning members (`binary_search_by_key` miss) are silently skipped — exactly like `Voter._vote()` skipping non-gauge pools without adjusting `_totalVoteWeight`. Because the multiplier baseline is never recomputed after dropping the invalid/non-winning entries, a winning candidate's Borda weight depends on how many *unrelated or non-winning* accounts a voter happened to list before it, not on the voter's actual intended ranking among valid targets. This is the same "denominator/weight computed over the unfiltered input, payout computed over the filtered subset" mismatch as the report.

### Impact Explanation
The corrupted value is `prime_votes[i].1`, the weighted stake total that decides `T::ChangeMembers::set_prime(prime)`. The elected `prime` member typically receives special treatment downstream (e.g. `pallet-collective`'s prime member counts as an implicit default-vote/tie-breaker for proposals), so systematically skewing which candidate wins the Borda count can bias governance outcomes without needing any privileged actor — any ordinary voter using `vote()` can shape the outcome purely through vote-array ordering. This is an "unauthorized influence over origin-adjacent governance selection" issue (Medium), not a direct fund-loss bug, but it fits the "runtime bug that compromises intended behavior" category in the impact gate.

### Likelihood Explanation
Likelihood is Medium: it requires no special access — `vote()` is a public, signed extrinsic with no validation that vote entries are current candidates, and the bug is triggered automatically every election round (`do_phragmen`, called from `on_initialize`) whenever any voter includes non-winning/future-candidate entries ahead of a real target, which the pallet's own documentation says is an expected, supported voting pattern.

### Recommendation
Compute the Borda multiplier from the position of the vote **within the filtered list of entries that are actually in `new_members_sorted_by_id`**, not from the raw index of the full `votes` array. Concretely, first filter/rank each voter's `votes` down to only entries that match winning members, then assign multipliers `MaxVotesPerVoter - filtered_position` over that filtered sequence, so that voting for invalid/non-winning/future candidates cannot dilute or shift the weight given to entries that do win.

### Proof of Concept
1. Two candidates A and B end up winning the round (`new_members_sorted_by_id = [A, B]`).
2. Voter V1 (stake = 100) calls `vote(votes = [A, B], value = 100)` → A gets multiplier `MaxVotesPerVoter`, B gets `MaxVotesPerVoter - 1`.
3. Voter V2 (stake = 100), who actually wants to rank B highest, calls `vote(votes = [X, Y, B, A], value = 100)` where `X, Y` are non-candidates/future-candidates (allowed by `vote()`). Because `X, Y` occupy the first two Borda slots and are dropped silently by the `binary_search_by_key` miss, B is scored with multiplier `MaxVotesPerVoter - 2` and A with `MaxVotesPerVoter - 3` — both diluted, and B (V2's actual first preference) ends up scored *lower* than A even though V2 intended B to outrank A.
4. Aggregating over `voters_and_stakes`, the final `prime` selection in `prime_votes.into_iter().max_by_key(|x| x.1)` can therefore select a different, unintended candidate purely because of how many filler/invalid entries preceded the real picks — with no revert, no error, and no privileged action, mirroring the `_vote()` pool-vote dilution described in the source report.