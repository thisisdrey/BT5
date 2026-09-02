### Title
`simulate_intents` reports `Ok` against a stale `CachedState` snapshot while `execute_intents` re-validates against live mutable state, causing divergent outcomes (including panics) - (File: contracts/defuse/src/contract/intents/mod.rs)

### Summary
`simulate_intents` runs the batch through `Engine::new(self.cached(), ...)`, a `CachedState` wrapper that lazily forwards to `self.balance_of`/`self.is_nonce_used` read at simulate-call time, and only reports `InvariantViolated` without panicking on other errors turning to a panic anyway. `execute_intents` re-executes the identical `MultiPayload` batch against the real, current mutable state at a strictly later block/transaction. Any state-changing transaction landing between the two calls (e.g. a different valid nonce from the same signer being relayed by someone else, changing the signer's balance for token `T`) can make `execute_intents` diverge from what `simulate_intents` reported, including turning a clean `Ok` simulate result into a runtime panic in `execute_intents`.

### Finding Description
The binding claimed to hold is:
`simulate_intents(batch).invariant_violated == None` **implies** `execute_intents(batch)` succeeds with the same `Transfers`.

This binding is not enforced by any lock, versioning, or optimistic-concurrency check:

- `simulate_intents` builds `Engine::new(self.cached(), &mut inspector)` where `self.cached()` wraps `&Contract` (the view-only `self`) in `CachedState`, per `contracts/defuse/core/src/engine/state/mod.rs:56-62` and `contracts/defuse/core/src/engine/state/cached.rs:99-105` (`balance_of` falls back to `self.view.balance_of(...)` read live at simulate time), `contracts/defuse/src/contract/intents/mod.rs:44-64`.
- Errors other than `InvariantViolated` cause `err.panic()` inside the view call itself (`contracts/defuse/src/contract/intents/mod.rs:52-53`), so a simulate that returns cleanly (`invariant_violated: None`) means: at simulate time, balances were sufficient and there was no unmatched delta.
- `execute_intents` performs the identical checks (`internal_sub_balance` -> `CachedState`... no, real `Contract` state directly) but against the live, current state at the (later) execute transaction's time, via `Engine::new(self, ExecuteInspector::default())`, `contracts/defuse/src/contract/intents/mod.rs:27-30`. Any error, including `DefuseError::BalanceOverflow` from `internal_sub_balance` (`contracts/defuse/core/src/engine/state/cached.rs:249-252` pattern mirrored in the non-cached real `State` impl) or `InvariantViolated`, is turned into a panic via `.unwrap_or_else(|e| e.panic())`.
- Nothing prevents another valid, already-signed `MultiPayload` from the same `signer_id` (a different `Nonce`) — held by a different unprivileged party (e.g., a competing solver, as shown by the `solver_user_closure` pattern in `tests/src/tests/defuse/intents/token_diff.rs:375-472`) — from landing between the victim's `simulate_intents` call and their `execute_intents` call, changing the signer's balance for token `T` or committing a nonce touching the same funds.

Because `simulate_intents` and `execute_intents` are two separate NEAR calls separated in time with no shared lock, the "state" they operate over is not guaranteed identical, and the code provides no mechanism (block height, state root, or version check) to detect or reject a stale simulate result.

### Impact Explanation
No funds are stolen or duplicated; the Verifier's own invariant checks (`TransferMatcher::finalize`, `internal_sub_balance` balance checks) still hold for whichever state is actually mutated in `execute_intents`. The impact is exactly the "High" category explicitly defined by the rubric: `simulate_intents` reporting an outcome that `execute_intents` does not produce, misleading a party (e.g. a solver or relayer) who settles off-chain (e.g., releases counter-assets, quotes a price, or skips a manual re-check) based on the simulate result. The blast radius is scoped to whichever batch/signer was simulated and is repeatable across any batch/signer/token where a second, independent state-changing transaction can be interleaved between simulate and execute.

### Likelihood Explanation
Requires: (1) a signer with at least one other valid, unexpired, unused nonce/intent touching the same token `T` that can be submitted by any unprivileged relayer/solver independent of the party running simulate+execute, or any other unprivileged actor whose action changes `T`'s balance/nonce state for the signer between the two calls; (2) enough of a time gap between `simulate_intents` and `execute_intents` (two separate transactions) for a third transaction to land — trivial on a public network with concurrent solvers/relayers, which is the exact multi-solver RFQ pattern the codebase's own tests exercise (`solver_user_closure`). No special role or privilege is needed by the intervening party; only a validly signed payload from the signer (already obtained, e.g. as an alternate quote) or their own independent transaction is required. This is a standard TOCTOU race, not a logic bug in the invariant-matching arithmetic itself.

### Recommendation
- Document `simulate_intents` explicitly as a best-effort, point-in-time preview with no execution guarantee, and have callers (solvers/relayers) treat a stale simulate result as advisory only, always re-simulating immediately before executing (best-effort mitigation only, cannot fully eliminate the race).
- Consider adding an optional "expected pre-state" fingerprint (e.g., relevant nonce/balance hash or `current_salt`) to `execute_intents`/`simulate_intents` so `execute_intents` can atomically abort (return a typed error instead of an uncontrolled panic) if the live state has diverged from what was simulated, rather than surfacing a raw panic in `execute_intents`.
- At minimum, replace the blanket `.unwrap_or_else(|e| e.panic())` in `execute_intents` (`contracts/defuse/src/contract/intents/mod.rs:30`) with error handling that surfaces a structured, non-panicking failure for `InvariantViolated`/`BalanceOverflow` so downstream integrators can distinguish "stale simulate" races from unexpected contract bugs.

### Proof of Concept
```rust
// tests/src/tests/defuse/intents/simulate_toctou.rs (new near-workspaces sandbox test)
#[rstest]
#[tokio::test]
async fn simulate_execute_diverge_on_intervening_tx(#[future(awt)] env: Env) {
    let (user1, relayer, ft1) = futures::join!(env.create_user(), env.create_user(), env.create_token());
    env.initial_ft_storage_deposit(vec![user1.account_id()], vec![ft1.contract_id()]).await;

    // user1 deposits 1000 of ft1
    env.defuse_ft_deposit_to(ft1.contract_id(), 1000, user1.account_id(), None).await.unwrap();
    let token_id = TokenId::from(Nep141TokenId::new(ft1.contract_id().clone()));

    // user1 signs TWO independent withdraw intents (different nonces) each withdrawing 1000 of ft1
    let withdraw_a = user1.sign_defuse_payload_default(&env.defuse,
        [FtWithdraw { token: ft1.contract_id().clone(), receiver_id: user1.account_id().clone(), amount: 1000.into(), .. }]).await.unwrap();
    let withdraw_b = user1.sign_defuse_payload_default(&env.defuse,
        [FtWithdraw { token: ft1.contract_id().clone(), receiver_id: user1.account_id().clone(), amount: 1000.into(), .. }]).await.unwrap();

    // Step 1: simulate withdraw_a against current balance (1000) -> Ok, invariant_violated == None
    let sim = env.defuse.simulate_intents(MultiPayloadArgs { signed: &[withdraw_a.clone()] }).await.unwrap();
    assert!(sim.invariant_violated.is_none());

    // Step 2: intervening transaction from an unprivileged relayer executes withdraw_b first,
    // draining user1's real ft1 balance to 0
    env.defuse_execute_intents(env.defuse.contract_id(), vec![withdraw_b]).await.unwrap();

    // Step 3: execute withdraw_a against the now-changed real state -> panics (BalanceOverflow),
    // diverging from the earlier simulate's Ok/None result
    let result = env.defuse_execute_intents(env.defuse.contract_id(), vec![withdraw_a]).await;
    assert!(result.is_err(), "execute_intents panicked, diverging from prior simulate_intents Ok result");
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/defuse/src/contract/intents/mod.rs (L24-64)
```rust
#[near]
impl Intents for Contract {
    #[pause(name = "intents")]
    fn execute_intents(&mut self, signed: Vec<MultiPayload>) {
        if let Some(event) = Engine::new(self, ExecuteInspector::default())
            .execute_signed_intents(signed)
            .unwrap_or_else(|e| e.panic())
            .as_mt_event()
        {
            // NOTE: Not all `mt_transfer` events are refundable, but it's safe to check them
            // all at once since non-refundable transfers only increase the potential refund
            // log size without affecting correctness. This can actually prevent resolve transfer
            // from failing due to too long event log !!!
            event
                .check_refund()
                .unwrap_or_else(|err| err.panic())
                .emit();
        }
    }

    #[pause(name = "intents")]
    fn simulate_intents(&self, signed: Vec<MultiPayload>) -> SimulationOutput {
        let mut inspector = SimulateInspector::default();
        let engine = Engine::new(self.cached(), &mut inspector);

        let invariant_violated = match engine.execute_signed_intents(signed) {
            // do not log transfers
            Ok(_) => None,
            Err(DefuseError::InvariantViolated(v)) => Some(v),
            Err(err) => err.panic(),
        };

        SimulationOutput {
            report: inspector.into_report(),
            invariant_violated,
            state: StateOutput {
                fee: self.fee(),
                current_salt: self.salts.current(),
            },
        }
    }
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L99-105)
```rust
    fn balance_of(&self, account_id: &AccountIdRef, token_id: &TokenId) -> u128 {
        self.accounts
            .get(account_id)
            .map(Lock::as_inner_unchecked)
            .and_then(|account| account.token_amounts.get(token_id).copied())
            .unwrap_or_else(|| self.view.balance_of(account_id, token_id))
    }
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L226-255)
```rust
    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        token_amounts: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let account = self
            .accounts
            .get_or_create(owner_id.to_owned(), |owner_id| {
                self.view.is_account_locked(owner_id)
            })
            .get_mut()
            .ok_or_else(|| DefuseError::AccountLocked(owner_id.to_owned()))?;
        for (token_id, amount) in token_amounts {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            if account.token_amounts.get(&token_id).is_none() {
                account
                    .token_amounts
                    .add(token_id.clone(), self.view.balance_of(owner_id, &token_id))
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
            account
                .token_amounts
                .sub(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }
        Ok(())
    }
```

**File:** contracts/defuse/core/src/engine/state/mod.rs (L56-62)
```rust
    #[inline]
    fn cached(self) -> CachedState<Self>
    where
        Self: Sized,
    {
        CachedState::new(self)
    }
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L375-463)
```rust
#[rstest]
#[trace]
#[tokio::test]
async fn solver_user_closure(
    #[values(Pips::ZERO, Pips::ONE_BIP, Pips::ONE_PERCENT)] fee: Pips,
    #[notrace]
    #[with(Env::builder().fee(fee))]
    #[future(awt)]
    env: Env,
) {
    const USER_BALANCE: u128 = 1100;
    const SOLVER_BALANCE: u128 = 2100;

    // RFQ: 1000 token_in -> ??? token_out
    const USER_DELTA_IN: i128 = -1000;

    let (user, solver, ft1, ft2) = futures::join!(
        env.create_user(),
        env.create_user(),
        env.create_token(),
        env.create_token()
    );

    env.initial_ft_storage_deposit(
        vec![user.account_id(), solver.account_id()],
        vec![ft1.contract_id(), ft2.contract_id()],
    )
    .await;

    // deposit
    futures::try_join!(
        env.defuse_ft_deposit_to(ft1.contract_id(), USER_BALANCE, user.account_id(), None),
        env.defuse_ft_deposit_to(ft2.contract_id(), SOLVER_BALANCE, solver.account_id(), None)
    )
    .expect("Failed to deposit tokens");

    let token_in = TokenId::from(Nep141TokenId::new(ft1.contract_id().clone()));
    let token_out = TokenId::from(Nep141TokenId::new(ft2.contract_id().clone()));

    dbg!(USER_DELTA_IN);
    // propagate RFQ to solver with adjusted amount_in
    let solver_delta_in = TokenDiff::closure_delta(&token_in, USER_DELTA_IN, fee).unwrap();

    // assume solver trades 1:2
    let solver_delta_out = solver_delta_in * -2;
    dbg!(solver_delta_in, solver_delta_out);

    // solver signs his intent
    let solver_commitment = solver
        .sign_defuse_payload_default(
            &env.defuse,
            [TokenDiff {
                diff: TokenDeltas::new(
                    [
                        (token_in.clone(), solver_delta_in),
                        (token_out.clone(), solver_delta_out),
                    ]
                    .into_iter()
                    .collect(),
                ),
                memo: None,
                referral: None,
            }],
        )
        .await
        .unwrap();

    // simulate before returning quote
    let simulation_before_return_quote = env
        .defuse
        .simulate_intents(MultiPayloadArgs {
            signed: &[solver_commitment.clone()],
        })
        .await
        .unwrap();
    println!(
        "simulation_before_return_quote: {}",
        serde_json::to_string_pretty(&simulation_before_return_quote).unwrap()
    );

    // we expect unmatched deltas to correspond with user_delta_in and
    // user_delta_out and fee
    let unmatched_deltas = simulation_before_return_quote
        .invariant_violated
        .unwrap()
        .into_unmatched_deltas()
        .unwrap();
    // there should be unmatched deltas only for 2 tokens: token_in and token_out
    assert_eq!(unmatched_deltas.len(), 2);
```
