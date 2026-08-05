Confirmed: `pallet_referenda` reads `Balances::InactiveIssuance` when servicing referenda (`nudge_referendum_continue_not_confirming`/`_continue_confirming`, per the weight annotations), i.e. active issuance (`total_issuance - inactive_issuance`) feeds into governance tally/threshold arithmetic. This corroborates the analog below.

### Title
Unauthenticated deposits to the DAP staging account are trusted as legitimate burns, letting anyone deflate `InactiveIssuance`/active issuance at will - (File: `substrate/frame/dap/src/lib.rs`)

### Summary
`pallet-dap`'s `on_idle` hook drains whatever `reducible_balance` sits in the deterministically-derived staging account and treats the *entire amount* as a legitimate slash/burn credit, calling `Unbalanced::deactivate()` on it. Nothing binds that balance to an actual `OnUnbalanced` event; any unprivileged account can transfer ordinary funds to the publicly known staging account and have them counted as "deactivated" issuance, exactly mirroring the ERC20BalanceGteEnforcer flaw where a raw balance delta is trusted as proof of an intended event without validating its provenance.

### Finding Description
`Pallet::staging_account()` is a deterministic sub-account: [1](#0-0) . Legitimate flows deposit slashes/burns into it via the `OnUnbalanced` impl: [2](#0-1) .

`on_idle` then reads whatever `reducible_balance` (Preserve/Polite) is present in that account — with no way to distinguish attacker-sent funds from real slash credits — transfers it to the buffer, and deactivates the same amount: [3](#0-2) 

Because `staging_account()` is just a regular `AccountId` (derived via `into_sub_account_truncating`), any signed account can call `pallet_balances::transfer_keep_alive` (or any other transfer) to it. The pallet has no check that the balance increase actually originated from a wired `OnUnbalanced` source — it is functionally identical to the ERC20BalanceGteEnforcer's flaw: "balance present == intended event happened," with no binding to the sender/origin of the transfer.

The conformance test confirms the semantics of `deactivate`: it lowers `active_issuance` (`total_issuance - InactiveIssuance`) while leaving `total_issuance` untouched: [4](#0-3) . `Balances::InactiveIssuance` is read by `pallet-referenda`'s servicing logic, per the storage-access annotations on `nudge_referendum_continue_not_confirming`/`_continue_confirming`: [5](#0-4) , meaning active issuance is a governance-critical figure used to compute vote support/approval thresholds ("Incoming funds are deactivated to exclude them from governance voting," per the pallet's own doc comment: [6](#0-5) ).

### Impact Explanation
Any unprivileged holder of a small amount of funds can send an arbitrary, self-chosen amount to a publicly-derivable account address and have the runtime silently record it as "deactivated" issuance without any real slash or burn occurring. This lets an attacker artificially and repeatedly deflate `active_issuance` — the denominator/input used by OpenGov referenda support/approval calculations — degrading the intended-behavior guarantee that active issuance accurately reflects funds excluded from governance for legitimate reasons (slashes/burns/dust). This is a runtime bug that compromises intended accounting behavior (governance-threshold correctness), achievable without any privileged role, matching the "runtime bugs that compromise intended behavior" and state-integrity criteria in scope.

### Likelihood Explanation
High feasibility, low cost of entry: `staging_account()` is deterministic and exposed via a view function (`Pallet::staging()`), so no guessing is required. The only requirement is an ordinary signed account and a `pallet_balances::transfer` call — no governance, no validator/collator privilege, no relayer or malicious peer assumption. The main cost to the attacker is the transferred funds themselves (which move into the buffer account rather than being returned), so the attack is not free, but it is entirely mechanical and repeatable by any single actor at any time (bounded only by their own balance), with `on_idle` processing it automatically on the next idle block.

### Recommendation
Do not treat "whatever balance sits in the staging account" as ground truth for the amount to deactivate. Instead, track deactivation amounts explicitly at the point of `OnUnbalanced::on_nonzero_unbalanced` (e.g., accumulate a dedicated `PendingDeactivation` storage counter incremented only by the trusted burn/slash callback), and have `on_idle` drain/deactivate exactly that tracked amount rather than the account's raw `reducible_balance`. Alternatively, reject/refuse ordinary `transfer`-sourced deposits into the staging account (e.g., by using an account type that cannot receive normal transfers), so only the pallet's own `OnUnbalanced` credit path can fund it.

### Proof of Concept
1. Query `pallet_dap::Pallet::<Runtime>::staging()` (or derive `sp_dap::DAP_PALLET_ID.into_sub_account_truncating(sp_dap::DAP_STAGING_ACCOUNT_ID)`).
2. From any funded, unprivileged account, submit `Balances::transfer_keep_alive(staging_account, X)` for an arbitrary `X`.
3. Wait for/trigger `pallet_dap::on_idle` (runs automatically on idle blocks).
4. Observe: `buffer_account` balance increases by `X`, and `Balances::InactiveIssuance` increases by `X` — identical to the legitimate slash path shown in [7](#0-6)  — even though no slash, burn, or any wired `OnUnbalanced` event occurred; the deposit was an ordinary user-initiated transfer.

### Citations

**File:** substrate/frame/dap/src/lib.rs (L29-32)
```rust
//! - **Burn Collection**: Implements `OnUnbalanced` to intercept any burn source wired to it
//!   (staking slashes, transaction fees, dust removal, EVM gas rounding, etc.) and redirect funds
//!   into the buffer account. Incoming funds are deactivated to exclude them from governance
//!   voting.
```

**File:** substrate/frame/dap/src/lib.rs (L214-256)
```rust
		fn on_idle(_block: BlockNumberFor<T>, remaining_weight: Weight) -> Weight {
			let mut meter = WeightMeter::with_limit(remaining_weight);

			// Need at least one read (staging account balance).
			if meter.try_consume(T::DbWeight::get().reads(1)).is_err() {
				return meter.consumed();
			}

			let staging_account = Self::staging_account();
			let available = T::Currency::reducible_balance(
				&staging_account,
				Preservation::Preserve,
				Fortitude::Polite,
			);

			if available.is_zero() {
				return meter.consumed();
			}

			// Need 1 read and 2 writes for the transfer, plus 1 read and 1 write for
			// deactivate (InactiveIssuance) and 1 read for TotalIssuance.
			if meter.try_consume(T::DbWeight::get().reads_writes(3, 3)).is_err() {
				return meter.consumed();
			}

			let buffer = Self::buffer_account();
			if T::Currency::transfer(&staging_account, &buffer, available, Preservation::Preserve)
				.is_err()
			{
				defensive!("DAP: staging account transfer to buffer failed");
				return meter.consumed();
			}

			Self::deactivate_buffer_funds(available);
			Self::deposit_event(Event::StagingDrained { amount: available });

			log::debug!(
				target: LOG_TARGET,
				"DAP: drained {available:?} from staging account to DAP buffer"
			);

			meter.consumed()
		}
```

**File:** substrate/frame/dap/src/lib.rs (L351-357)
```rust
		/// The DAP staging account.
		///
		/// Incoming funds land here and are periodically drained and deactivated into the
		/// DAP buffer account by `on_idle`.
		pub fn staging_account() -> T::AccountId {
			sp_dap::DAP_PALLET_ID.into_sub_account_truncating(sp_dap::DAP_STAGING_ACCOUNT_ID)
		}
```

**File:** substrate/frame/dap/src/lib.rs (L512-529)
```rust
impl<T: Config> OnUnbalanced<CreditOf<T>> for Pallet<T> {
	fn on_nonzero_unbalanced(amount: CreditOf<T>) {
		let staging = Self::staging_account();
		let numeric_amount = amount.peek();

		// Funds land in the staging account; `on_idle` will drain them into the buffer and
		// deactivate them there.  Deactivation is intentionally deferred so that active issuance
		// does not flicker down-then-up within the same block.
		let _ = T::Currency::resolve(&staging, amount).inspect_err(|_| {
			defensive!(
				"🚨 Failed to deposit slash to DAP staging account - funds burned, it should never happen!"
			);
		});
		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited {numeric_amount:?} to DAP staging account"
		);
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungible/conformance_tests/regular/unbalanced.rs (L247-260)
```rust
/// Tests [`Unbalanced::deactivate`] and [`Unbalanced::reactivate`].
pub fn deactivate_and_reactivate<T, AccountId>()
where
	T: Unbalanced<AccountId>,
	<T as Inspect<AccountId>>::Balance: AtLeast8BitUnsigned + Debug,
	AccountId: AtLeast8BitUnsigned,
{
	T::set_total_issuance(10.into());
	assert_eq!(T::total_issuance(), 10.into());
	assert_eq!(T::active_issuance(), 10.into());

	T::deactivate(2.into());
	assert_eq!(T::total_issuance(), 10.into());
	assert_eq!(T::active_issuance(), 8.into());
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/weights/pallet_referenda.rs (L427-442)
```rust
	/// Storage: Referenda ReferendumInfoFor (r:1 w:1)
	/// Proof: Referenda ReferendumInfoFor (max_values: None, max_size: Some(936), added: 3411, mode: MaxEncodedLen)
	/// Storage: Balances InactiveIssuance (r:1 w:0)
	/// Proof: Balances InactiveIssuance (max_values: Some(1), max_size: Some(16), added: 511, mode: MaxEncodedLen)
	/// Storage: Scheduler Agenda (r:1 w:1)
	/// Proof: Scheduler Agenda (max_values: None, max_size: Some(38963), added: 41438, mode: MaxEncodedLen)
	fn nudge_referendum_continue_not_confirming() -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `400`
		//  Estimated: `42428`
		// Minimum execution time: 28_594_000 picoseconds.
		Weight::from_parts(29_092_000, 0)
			.saturating_add(Weight::from_parts(0, 42428))
			.saturating_add(T::DbWeight::get().reads(3))
			.saturating_add(T::DbWeight::get().writes(2))
	}
```

**File:** substrate/frame/dap/src/tests/on_unbalanced.rs (L85-144)
```rust
#[test]
fn slash_to_dap_accumulates_to_staging_then_deactivates_on_idle() {
	build_and_execute(true, || {
		set_default_budget_allocation();

		let buffer = DapPallet::buffer_account();
		let staging = DapPallet::staging_account();
		let ed = <Balances as Inspect<_>>::minimum_balance();

		let alice = account_id(1);
		let bob = account_id(2);
		let charlie = account_id(3);

		// Given: buffer and staging each have ED; users have balances.
		assert_eq!(Balances::free_balance(&buffer), ed);
		assert_eq!(Balances::free_balance(&staging), ed);
		let initial_active = <Balances as Inspect<_>>::active_issuance();
		let initial_total = <Balances as Inspect<_>>::total_issuance();

		// When: multiple slashes occur via OnUnbalanced (simulating staking slashes).
		for (who, amount) in [(&alice, 30u64), (&bob, 20), (&charlie, 50)] {
			let credit = <Balances as Balanced<_>>::withdraw(
				who,
				amount,
				Precision::Exact,
				Preservation::Preserve,
				Fortitude::Force,
			)
			.unwrap();
			DapPallet::on_unbalanced(credit);
		}

		// Then: funds land in staging, not buffer.
		assert_eq!(Balances::free_balance(&staging), ed + 100);
		assert_eq!(Balances::free_balance(&buffer), ed);

		// And: users lost their slashed amounts.
		assert_eq!(Balances::free_balance(&alice), 100 - 30);
		assert_eq!(Balances::free_balance(&bob), 200 - 20);
		assert_eq!(Balances::free_balance(&charlie), 300 - 50);

		// And: active issuance is NOT yet decreased (deactivation is deferred to on_idle).
		assert_eq!(<Balances as Inspect<_>>::active_issuance(), initial_active);

		// And: total issuance unchanged (funds moved, not destroyed).
		assert_eq!(<Balances as Inspect<_>>::total_issuance(), initial_total);

		// When: on_idle drains staging into buffer and deactivates.
		DapPallet::on_idle(1, Weight::MAX);

		// Then: staging retains only ED; buffer gained all slashed funds.
		assert_eq!(Balances::free_balance(&staging), ed);
		assert_eq!(Balances::free_balance(&buffer), ed + 100);

		// And: active issuance decreased by 100 (funds deactivated in DAP buffer).
		assert_eq!(<Balances as Inspect<_>>::active_issuance(), initial_active - 100);

		// And: total issuance still unchanged.
		assert_eq!(<Balances as Inspect<_>>::total_issuance(), initial_total);
	});
```
