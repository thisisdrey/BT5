### Title
Missing minimum-value check on `inheritance_delay` lets the account owner set a zero-length recovery time-lock, defeating the pallet's malicious-recovery protection - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
`pallet-recovery`'s `bound_friend_groups` sanity-checks every `FriendGroup` field that is security-relevant (non-empty friends, sorted/unique friends, `friends_needed` bounds) and explicitly rejects a zero `cancel_delay` via `Error::NoCancelDelay`, but it never rejects a zero `inheritance_delay`. This mirrors the Fraxlend finding's broken invariant: a delay/time-lock value that is supposed to give the protected party time to react can be freely set to a value that makes the lock meaningless, even though the code clearly treats the delay as a security control elsewhere.

### Finding Description
`FriendGroup::inheritance_delay` is documented as "Minimum time that a recovery attempt must stay active before it can be finished" [1](#0-0) , and `finish_attempt` enforces it via `ensure!(now >= inheritable_at, Error::<T>::NotYetInheritable)` where `inheritable_at = init_block + inheritance_delay` [2](#0-1) . The pallet's own README explicitly relies on this delay as the mechanism that lets the true account owner detect and slash a malicious recovery attempt before it completes: "Configure a significant `delay_period` for your recovery process: As long as you have access to your recoverable account, you need only check the blockchain once every `delay_period` blocks…" [3](#0-2) .

However, the validation function that sanity-checks a `FriendGroup` before storing it, `bound_friend_groups`, only enforces `cancel_delay != 0` (with the comment "prevent mempool frontrunning by requiring at least 1 block") and performs no equivalent check on `inheritance_delay`: [4](#0-3) 

This is called from `set_friend_groups`, the only extrinsic that lets an account configure its own recovery friend groups [5](#0-4) . Because `inheritance_delay` is a `ProvidedBlockNumber` with a `Default` of zero and no lower-bound check, a `FriendGroup` with `inheritance_delay = 0` passes validation and is stored. If such a group's friends all approve in the same block the attempt was initiated (`initiate_attempt` immediately records the initiator's approval, so a `friends_needed = 1` group is fully approved on init), then `init_block == now`, so `inheritable_at = init_block == now`, and `finish_attempt` succeeds instantly — the lost account gets zero window to notice and call `slash_attempt` before the inheritor takeover is finalized.

This is the direct analog of the external report's broken invariant: a delay parameter meant to enforce a real time-lock between two on-chain actions has no minimum enforced, and it is entirely attacker-controllable (here, by whoever set up the friend groups, including someone who compromised the account only long enough to call `set_friend_groups`, or a user tricked into importing/signing a malicious recovery configuration). Just as the Fraxlend owner could point `TIME_LOCK_ADDRESS` at a contract with no delay to silently defeat the fee-change protection, here the configuration path silently accepts a delay of `0`, which functionally removes the protection `finish_attempt` is supposed to enforce.

### Impact Explanation
An account whose friend-group configuration ends up with `inheritance_delay = 0` (e.g., set by an attacker who briefly compromises the signing key, or via a maliciously crafted "recommended" configuration that a user copies without realizing the delay is unenforced-at-minimum) loses the intended reaction window entirely. A colluding "friend" set can then take over the account's `Inheritor` slot and drain funds via `control_inherited_account` in the same block the attempt is opened, with no possibility for the lost account to call `slash_attempt` in time. This directly undermines the "Malicious Recovery Attempts" mitigation the pallet documentation relies on, causing potential unbacked loss of the recovered account's held value to an unintended beneficiary — a fund-theft/lock impact in scope.

### Likelihood Explanation
Likelihood is moderate: it requires the friend-group configuration for a given account to actually contain `inheritance_delay = 0`, which normally only happens through misconfiguration, a compromised signing key used briefly, or a maliciously supplied recovery template. It does not require any governance, root, or validator/collator privilege — any account can trigger it via the plain `set_friend_groups` extrinsic, and the inconsistency (checked for `cancel_delay`, not checked for `inheritance_delay`) is a genuine oversight rather than requiring privileged abuse.

### Recommendation
Add a symmetrical minimum check in `bound_friend_groups`, e.g. `ensure!(!friend_group.inheritance_delay.is_zero(), Error::<T>::NoInheritanceDelay)`, mirroring the existing `NoCancelDelay` check, and consider enforcing a project-chosen minimum greater than zero blocks to guarantee a meaningful reaction window regardless of `friends_needed`/instant-approval race conditions.

### Proof of Concept
1. Account `ALICE` calls `set_friend_groups` with a single `FriendGroup { friends: [BOB], friends_needed: 1, inheritor: MALLORY, inheritance_delay: 0, inheritance_priority: 0, cancel_delay: 1 }` — this passes `bound_friend_groups` validation because only `cancel_delay` is checked [6](#0-5) .
2. `BOB` calls `initiate_attempt(ALICE, 0)`; since `friends_needed == 1` and the initiator's approval is auto-recorded, the attempt is immediately fully approved [7](#0-6) .
3. Anyone calls `finish_attempt(ALICE, 0)` in the same block: `inheritable_at = init_block + 0 == now`, so `ensure!(now >= inheritable_at, …)` passes trivially, and `MALLORY` becomes the inheritor of `ALICE`'s account [8](#0-7)  — with zero blocks elapsed for `ALICE` to notice and call `slash_attempt`.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L166-169)
```rust
	/// Minimum time that a recovery attempt must stay active before it can be finished.
	///
	/// Uses a provided block number to avoid possible clock skew of parachains.
	pub inheritance_delay: ProvidedBlockNumber,
```

**File:** substrate/frame/recovery/src/lib.rs (L630-647)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::set_friend_groups())]
		pub fn set_friend_groups(
			origin: OriginFor<T>,
			friend_groups: Vec<FriendGroupOf<T>>,
		) -> DispatchResult {
			let lost = ensure_signed(origin)?;

			if Attempt::<T>::iter_prefix(&lost).next().is_some() {
				return Err(Error::<T>::HasOngoingAttempts.into());
			}

			let (old_friend_groups, old_ticket) = match FriendGroups::<T>::get(&lost) {
				Some((g, t)) => (g, Some(t)),
				None => Default::default(),
			};

			let new_friend_groups = Self::bound_friend_groups(&lost, friend_groups)?;
```

**File:** substrate/frame/recovery/src/lib.rs (L713-726)
```rust
			// The initiator counts as the first approval, so they don't have to sign twice.
			let approvals = ApprovalBitfield::default()
				.with_bits([initiator_index])
				.defensive_proof("initiator_index < friends.len() <= MaxFriendsPerConfig; qed")
				.unwrap_or_default();

			let now = T::BlockNumberProvider::current_block_number();
			let attempt = AttemptOf::<T> {
				friend_group_index,
				initiator: initiator.clone(),
				init_block: now,
				last_approval_block: now,
				approvals,
			};
```

**File:** substrate/frame/recovery/src/lib.rs (L827-847)
```rust
			let inheritable_at = attempt
				.init_block
				.checked_add(&friend_group.inheritance_delay)
				.ok_or(ArithmeticError::Overflow)?;
			ensure!(now >= inheritable_at, Error::<T>::NotYetInheritable);
			// NOTE: We dont need to check the cancel delay, since enough friends voted and we dont
			// assume fully malicious behavior.

			let inheritor = friend_group.inheritor;
			let inheritance_priority = friend_group.inheritance_priority;

			match Inheritor::<T>::get(&lost) {
				None => {
					let ticket = Self::inheritor_ticket(&caller)?;
					Inheritor::<T>::insert(&lost, (inheritance_priority, &inheritor, ticket));
					Self::deposit_event(Event::<T>::AttemptFinished {
						lost,
						friend_group_index,
						inheritor,
						previous_inheritor: None,
					});
```

**File:** substrate/frame/recovery/src/lib.rs (L1002-1022)
```rust
	/// Sanity check the friend groups and bound them into a bounded vector.
	pub fn bound_friend_groups(
		lost: &T::AccountId,
		mut friend_groups: Vec<FriendGroupOf<T>>,
	) -> Result<FriendGroupsOf<T>, Error<T>> {
		for friend_group in &mut friend_groups {
			ensure!(!friend_group.friends.is_empty(), Error::<T>::NoFriends);
			// cannot contain the lost account itself
			ensure!(!friend_group.friends.contains(&lost), Error::<T>::LostAccountInFriendGroup);
			ensure!(
				friend_group.friends.windows(2).all(|w| w[0] < w[1]),
				Error::<T>::FriendsNotSortedOrUnique
			);
			ensure!(
				friend_group.friends_needed as usize <= friend_group.friends.len(),
				Error::<T>::TooManyFriendsNeeded
			);
			ensure!(friend_group.friends_needed > 0, Error::<T>::NoFriendsNeeded);
			// prevent mempool frontrunning by requiring at least 1 block
			ensure!(!friend_group.cancel_delay.is_zero(), Error::<T>::NoCancelDelay);
		}
```

**File:** substrate/frame/recovery/README.md (L81-90)
```markdown
### Safety Considerations

It is important to note that this is a powerful pallet that can compromise the
security of an account if used incorrectly. Some recommended practices for users
of this pallet are:

- Configure a significant `delay_period` for your recovery process: As long as you
  have access to your recoverable account, you need only check the blockchain once
  every `delay_period` blocks to ensure that no recovery attempt is successful
  against your account. Using off-chain notification systems can help with this,
```
