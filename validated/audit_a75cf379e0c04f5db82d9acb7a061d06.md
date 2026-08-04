### Title
Public, unauthenticated donations to `pallet-dap`'s known staging sub-account are drained and deactivated as if they were legitimate slash/burn proceeds, letting anyone manipulate network-wide active issuance - ([File: substrate/frame/dap/src/lib.rs])

### Summary
`pallet-dap`'s `on_idle` hook treats the entire balance sitting in the pallet's `staging_account()` as trusted, legitimately-slashed/burned funds and unconditionally drains it into the buffer account and calls `deactivate_buffer_funds()` on the full amount. It never verifies that the balance actually originated from an authorized `OnUnbalanced::on_nonzero_unbalanced` credit (staking slashes, dust removal, tx fees, etc.). This mirrors the `ParticleExchange.sol` bug class: a state-changing effect (`_execRepayWithNft` / here, `deactivate_buffer_funds`) is triggered purely because a precondition-looking value is observed (NFT ownership / staging-account balance) without checking that the value was produced through the intended, authorized path.

### Finding Description
`staging_account()` is a deterministically derivable, public `AccountId` (a sub-account of `sp_dap::DAP_PALLET_ID`), and its address is explicitly exposed to off-chain callers via the `staging()` view function [1](#0-0) . It is an ordinary `AccountId`, so **any signed account can transfer arbitrary funds into it** using a normal `Balances::transfer`, exactly the way any smart contract can call `onERC721Received` without ever transferring an NFT.

`on_idle` then reads the reducible balance of that account and, without any check on where the funds came from, transfers the whole amount to the buffer account and deactivates it: [2](#0-1) 

Compare with the intended/authorized deposit path, `OnUnbalanced::on_nonzero_unbalanced`, which is the only mechanism meant to fund the staging account (slashes, fee burns, dust removal): [3](#0-2) 

Nothing distinguishes funds that arrived via this authorized credit path from funds an attacker deposits directly with an ordinary `transfer` call. `on_idle` cannot tell the difference — it only observes `reducible_balance(staging_account, ...)`, exactly analogous to `onERC721Received` only observing that the callback fired rather than verifying the NFT was actually transferred.

### Impact Explanation
`deactivate_buffer_funds` calls `Unbalanced::deactivate`, which increases `InactiveIssuance` and therefore lowers `ActiveIssuance` (`Total - Inactive`) without changing `TotalIssuance`. `ActiveIssuance` is a chain-wide economic parameter consumed by the NPoS reward curve and other issuance-sensitive logic in `pallet-staking-async`. By repeatedly transferring small amounts of the native token into the publicly-known `staging_account` and letting `on_idle` run, any unprivileged account can:
- Artificially and permanently deflate `ActiveIssuance` relative to `TotalIssuance`, at a cost of only the transferred principal (which is not burned — it lands intact in the buffer account, merely relabeled "inactive").
- Skew the stake/issuance ratio consumed by inflation/reward curve computations network-wide, degrading intended validator/nominator reward economics — this is exactly the "runtime bug that compromises intended behavior" class called out in the impact gate.
- Do this repeatedly and cheaply since the funds are not lost, they simply move to `buffer_account` and get relabeled; the attacker (or a colluding party controlling `buffer_account`'s downstream distribution) is not economically punished for triggering the mislabeling.

No malicious validator, collator, governance actor, or privileged role is required — a single ordinary signed account with knowledge of the (publicly exposed) `staging_account()` address suffices.

### Likelihood Explanation
High. The staging account address is not secret — it's deterministically derived from `sp_dap::DAP_PALLET_ID` and `DAP_STAGING_ACCOUNT_ID`, and is explicitly published via the `staging()` view function for off-chain clients. `on_idle` runs every block opportunistically (guarded only by a weight meter), so the drain-and-deactivate side effect happens automatically and repeatedly for whatever balance sits there, with zero authentication of provenance.

### Recommendation
Do not infer "this is legitimately slashed/burned value" purely from the balance present in `staging_account`. Instead:
- Track the amount credited via `OnUnbalanced::on_nonzero_unbalanced` in dedicated pallet storage (e.g. an accumulator incremented exactly by the amounts resolved into staging), and have `on_idle` drain/deactivate only up to that tracked amount, decrementing it accordingly.
- Alternatively, reconcile any balance in `staging_account` beyond the tracked legitimate amount as a no-op (leave it, or route it back to depositors / to a separate "unexpected funds" event) rather than silently deactivating it.
- Add a defensive event/log distinguishing "expected slash drain" from "unexpected staging inflow" so operators can detect griefing attempts, mirroring the recommendation to verify actual receipt before honoring the "repaid" state in the referenced report.

### Proof of Concept
1. Query `pallet_dap::Pallet::<Runtime>::staging()` (or compute `sp_dap::DAP_PALLET_ID.into_sub_account_truncating(sp_dap::DAP_STAGING_ACCOUNT_ID)`) to obtain the staging account address — this is public information by design.
2. From any funded account, submit `Balances::transfer_allow_death(origin, staging_account, X)` for an arbitrary amount `X`.
3. Wait for (or trigger) `pallet_dap::Pallet::<Runtime>::on_idle` to run, as exercised in the existing test harness at [4](#0-3) .
4. Observe: `buffer_account` balance increases by `X`, and `ActiveIssuance` decreases by `X` — identical on-chain effect to a legitimate slash of `X`, even though no slash, burn, or authorized `OnUnbalanced` call ever occurred. Repeating this at will lets an unprivileged account arbitrarily deflate `ActiveIssuance` relative to `TotalIssuance`.

### Citations

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

**File:** substrate/frame/dap/src/lib.rs (L334-339)
```rust
		/// Account that holds burned/slashed funds before they are drained into
		/// the DAP buffer by `on_idle`. Exposed to clients so they don't have to
		/// re-derive the sub-account themselves.
		pub fn staging() -> T::AccountId {
			Self::staging_account()
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

**File:** substrate/frame/dap/src/tests/on_unbalanced.rs (L132-144)
```rust
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
