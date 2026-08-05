## Analysis

The external report's core broken invariant is: **two independently-created time-locks that are supposed to be causally ordered (the "responder" lock must always outlast the "initiator" lock) have no on-chain enforcement of that ordering, and one party fully controls their own lock's duration.** This lets the party who chooses their own lock last/shortest reclaim their collateral risk-free via a refund/cancel path while the counterparty remains exposed for a much longer, unilaterally-fixed window.

I found a structural analog in `substrate/frame/atomic-swap`, which is Substrate's own on-chain hash-time-locked-swap primitive (the "hashed proof" is the HTLC hashlock, `end_block` is the timelock, `claim_swap`/`cancel_swap` are the redeem/refund equivalents). [1](#0-0) 

The pallet's own doc comment acknowledges the exact same asymmetric-timelock hazard as the external report, but only as a social recommendation — nothing in the code enforces it: [2](#0-1) 

`create_swap` takes an attacker-supplied `duration: BlockNumberFor<T>` with **no minimum-duration check, no relation to any other lock**, and immediately reserves the source's funds: [3](#0-2) 

`cancel_swap` only requires that the caller is the original `source` and that `block_number >= swap.end_block` — since `end_block = current_block + duration`, an attacker who sets `duration = 0` (or 1) can reclaim their own locked funds at the very next block, with zero risk: [4](#0-3) 

The pallet's own README/test demonstrate the intended two-chain usage pattern (one `PendingSwap` per chain, matched by the same `hashed_proof`), which is exactly the cross-chain HTLC pattern from the external report: [5](#0-4) 

Note: unlike the Solidity original, `claim_swap` binds the claimant to `target = ensure_signed(origin)`, so a bystander cannot claim someone else's lock. This means the analog cannot reproduce a *third-party* theft, but it does directly reproduce the **asymmetric-timelock fund-lock/griefing primitive**: the second locker can safely renege (refund) while the first locker's larger/longer lock is stuck, matching the "permanent user-fund lock" impact category.

### Title
Unenforced/asymmetric timelock duration in `pallet_atomic_swap::create_swap` allows risk-free reneging and permanent counterparty fund lock - (File: `substrate/frame/atomic-swap/src/lib.rs`)

### Summary
`create_swap` accepts an arbitrary, attacker-chosen `duration` with no minimum floor and no enforced relationship to the counterparty's matching swap duration in a two-party (often cross-chain) atomic swap. The pallet's own documentation acknowledges that the "revealer" must pick a shorter duration than the counterparty to be safe, but this is never checked in code. A malicious counterparty can therefore create their side of the swap with `duration = 0` (or any trivially small value), guaranteeing they can `cancel_swap` and fully reclaim their own reserved funds at essentially zero risk and zero cost, while the honest party's already-created swap (whose `end_block` was fixed before it could observe the counterparty's choice) remains locked for its full, much longer duration with no recourse.

### Finding Description
`PendingSwap::end_block` is computed purely from the caller-supplied `duration`: [6](#0-5) 
There is no `Config` constant enforcing a minimum duration and no check relating this swap's duration to any other swap (which is architecturally impossible for the pallet to know on its own, since the counterparty's swap may live on a different chain). The module's doc explicitly flags this exact risk as something the *caller* must manage manually: [7](#0-6) 
`cancel_swap` requires only `source == caller` and `block_number >= end_block`: [8](#0-7) 
Because `duration` can be `Zero::zero()`, `end_block` can equal the current block, letting the malicious party cancel and reclaim their locked funds in the very next block — this is the exact analog of the LP setting its `timelock` to the protocol's floor (15 minutes) in the external report, except here there isn't even a floor.

### Impact Explanation
In the intended two-party/cross-chain usage (as shown in the pallet's own tests), Alice locks funds first (long duration, chosen without knowledge of Bob's future choice), and Bob locks funds second, matching the same `hashed_proof`. If Bob sets a trivial `duration`, Bob can safely refund immediately, forcing Alice into an all-risk, no-reward position: Alice's funds stay locked for the full window she originally chose, she cannot get Bob's funds (his lock is already gone), and she cannot cancel her own swap early. This is a permanent/extended user-fund lock caused entirely by an unprivileged counterparty's choice of parameter, with no admin, relayer, or governance actor involved — matching the "permanent user-fund ... lock" impact category.

### Likelihood Explanation
High. `duration` is a fully attacker-controlled `BlockNumberFor<T>` parameter with no validation; any account acting as `source` in a two-leg swap can trivially set it to 0 or 1. No special privileges, front-running, or malicious infrastructure roles are required — only that the account plays the "second locker" role in an otherwise cooperative-looking swap.

### Recommendation
Add a `Config::MinimumSwapDuration` (or similar) floor enforced in `create_swap`, and/or require `duration` to be bounded relative to a runtime-configured maximum skew, similar to the report's remediation of forcing at least 2x the minimum timelock. At minimum, document and ideally enforce that pallet users building cross-instance/cross-chain swaps must relay and verify the counterparty's `duration` before locking, or add an optional `min_counterparty_duration` parameter that can be checked against relayed proof of the other leg where feasible.

### Proof of Concept
1. Alice (source) calls `create_swap(target = Bob, hashed_proof = H, action = reserve(100), duration = 1000)` on chain 1 — Alice's 100 tokens are reserved immediately.
2. Bob (source), acting maliciously, calls `create_swap(target = Alice, hashed_proof = H, action = reserve(100), duration = 0)` on chain 2 — Bob's 100 tokens are reserved.
3. At the very next block on chain 2, Bob calls `cancel_swap(target = Alice, hashed_proof = H)`; since `block_number >= end_block` (`end_block == current_block`), the swap is cancelled and Bob's 100 tokens are returned to him instantly — see `cancel_swap` check [9](#0-8) .
4. Alice, expecting a 1000-block window to reveal her proof, has effectively no window on chain 2 to claim Bob's funds before Bob reneges.
5. Alice's 100 tokens remain locked on chain 1 for the full 1000 blocks she originally committed to, with no way to recover early since only `source` (herself) can cancel and only after `end_block`.

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L59-83)
```rust
/// Pending atomic swap operation.
#[derive(
	Clone,
	Eq,
	PartialEq,
	DebugNoBound,
	Encode,
	Decode,
	DecodeWithMemTracking,
	TypeInfo,
	MaxEncodedLen,
)]
#[scale_info(skip_type_params(T))]
#[codec(mel_bound())]
pub struct PendingSwap<T: Config> {
	/// Source of the swap.
	pub source: T::AccountId,
	/// Action of this swap.
	pub action: T::SwapAction,
	/// End block of the lock.
	pub end_block: BlockNumberFor<T>,
}

/// Hashed proof type.
pub type HashedProof = [u8; 32];
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L247-280)
```rust
		/// - `target`: Receiver of the atomic swap.
		/// - `hashed_proof`: The blake2_256 hash of the secret proof.
		/// - `balance`: Funds to be sent from origin.
		/// - `duration`: Locked duration of the atomic swap. For safety reasons, it is recommended
		///   that the revealer uses a shorter duration than the counterparty, to prevent the
		///   situation where the revealer reveals the proof too late around the end block.
		#[pallet::call_index(0)]
		#[pallet::weight(T::DbWeight::get().reads_writes(1, 1).ref_time().saturating_add(40_000_000))]
		pub fn create_swap(
			origin: OriginFor<T>,
			target: T::AccountId,
			hashed_proof: HashedProof,
			action: T::SwapAction,
			duration: BlockNumberFor<T>,
		) -> DispatchResult {
			let source = ensure_signed(origin)?;
			ensure!(
				!PendingSwaps::<T>::contains_key(&target, hashed_proof),
				Error::<T>::AlreadyExist
			);

			action.reserve(&source)?;

			let swap = PendingSwap {
				source,
				action,
				end_block: frame_system::Pallet::<T>::block_number() + duration,
			};
			PendingSwaps::<T>::insert(target.clone(), hashed_proof, swap.clone());

			Self::deposit_event(Event::NewSwap { account: target, proof: hashed_proof, swap });

			Ok(())
		}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L324-352)
```rust
		/// Cancel an atomic swap. Only possible after the originally set duration has passed.
		///
		/// The dispatch origin for this call must be _Signed_.
		///
		/// - `target`: Target of the original atomic swap.
		/// - `hashed_proof`: Hashed proof of the original atomic swap.
		#[pallet::call_index(2)]
		#[pallet::weight(T::DbWeight::get().reads_writes(1, 1).ref_time().saturating_add(40_000_000))]
		pub fn cancel_swap(
			origin: OriginFor<T>,
			target: T::AccountId,
			hashed_proof: HashedProof,
		) -> DispatchResult {
			let source = ensure_signed(origin)?;

			let swap = PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::NotExist)?;
			ensure!(swap.source == source, Error::<T>::SourceMismatch);
			ensure!(
				frame_system::Pallet::<T>::block_number() >= swap.end_block,
				Error::<T>::DurationNotPassed,
			);

			swap.action.cancel(&swap.source);
			PendingSwaps::<T>::remove(&target, hashed_proof);

			Self::deposit_event(Event::SwapCancelled { account: target, proof: hashed_proof });

			Ok(())
		}
```

**File:** substrate/frame/atomic-swap/src/tests.rs (L65-130)
```rust
#[test]
fn two_party_successful_swap() {
	let mut chain1 = new_test_ext();
	let mut chain2 = new_test_ext();

	// A generates a random proof. Keep it secret.
	let proof: [u8; 2] = [4, 2];
	// The hashed proof is the blake2_256 hash of the proof. This is public.
	let hashed_proof = blake2_256(&proof);

	// A creates the swap on chain1.
	chain1.execute_with(|| {
		AtomicSwap::create_swap(
			RuntimeOrigin::signed(A),
			B,
			hashed_proof,
			BalanceSwapAction::new(50),
			1000,
		)
		.unwrap();

		assert_eq!(Balances::free_balance(A), 100 - 50);
		assert_eq!(Balances::free_balance(B), 200);
	});

	// B creates the swap on chain2.
	chain2.execute_with(|| {
		AtomicSwap::create_swap(
			RuntimeOrigin::signed(B),
			A,
			hashed_proof,
			BalanceSwapAction::new(75),
			1000,
		)
		.unwrap();

		assert_eq!(Balances::free_balance(A), 100);
		assert_eq!(Balances::free_balance(B), 200 - 75);
	});

	// A reveals the proof and claims the swap on chain2.
	chain2.execute_with(|| {
		AtomicSwap::claim_swap(
			RuntimeOrigin::signed(A),
			proof.to_vec(),
			BalanceSwapAction::new(75),
		)
		.unwrap();

		assert_eq!(Balances::free_balance(A), 100 + 75);
		assert_eq!(Balances::free_balance(B), 200 - 75);
	});

	// B use the revealed proof to claim the swap on chain1.
	chain1.execute_with(|| {
		AtomicSwap::claim_swap(
			RuntimeOrigin::signed(B),
			proof.to_vec(),
			BalanceSwapAction::new(50),
		)
		.unwrap();

		assert_eq!(Balances::free_balance(A), 100 - 50);
		assert_eq!(Balances::free_balance(B), 200 + 50);
	});
}
```
