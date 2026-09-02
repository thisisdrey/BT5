This confirms the question's own hypothesis is correct: there is no in-call TOCTOU window here.

### Title
No vulnerability: `is_valid_salt` cannot be invalidated mid-`execute_signed_intent` by an unprivileged caller - (`contracts/defuse/core/src/engine/mod.rs`)

### Summary
`Engine::verify_intent_nonce` checks `self.state.is_valid_salt(salt)` once via a pure read-through chain (`Deltas<S>` → `CachedState<W>`/`Contract` → `SaltRegistry::is_valid`), and `commit_nonce` happens immediately after within the same synchronous call with no intervening state mutation opportunity. Since salt rotation/invalidation (`update_current_salt`, `invalidate_salts`) requires `access_control_any(roles(Role::DAO, Role::SaltManager))` and is a separate top-level transaction, no unprivileged attacker action within a single `execute_signed_intent`/`execute_signed_intents` call can change salt validity between the check and the commit.

### Finding Description
The claimed broken binding would be: `is_valid_salt_at_check(salt) == is_valid_salt_at_commit(salt)` failing within one call. Tracing the path: `Engine::execute_signed_intent` at [1](#0-0)  calls `verify_intent_nonce`, which reads `self.state.is_valid_salt(salt)` exactly once at [2](#0-1) , immediately followed synchronously (no promise, no yield) by `self.state.commit_nonce(...)`. `self.state` is `Deltas<S>`, whose `is_valid_salt` is a pure delegation to `self.state.is_valid_salt(salt)` [3](#0-2) , which for the on-chain contract further delegates through `CachedState<W>::is_valid_salt` (also pure delegation) [4](#0-3)  down to `Contract::is_valid_salt` → `self.salts.is_valid(salt)` [5](#0-4) , and finally `SaltRegistry::is_valid` [6](#0-5) . None of these layers write to storage or cache the salt registry's mutable state — they are all `&self` read methods. The only mutators of `SaltRegistry` (`set_new`, `invalidate`) are exposed exclusively through `SaltManager::update_current_salt` / `invalidate_salts`, both gated by `#[access_control_any(roles(Role::DAO, Role::SaltManager))]` [7](#0-6) . A NEAR contract call executes single-threaded to completion (no interleaving with another transaction), and `execute_intents`/`simulate_intents` never invoke `update_current_salt`/`invalidate_salts` internally [8](#0-7) . Therefore no unprivileged attacker payload, deposit `msg`, or intent execution path can mutate the salt registry between the `is_valid_salt` check and the `commit_nonce` call within one `execute_signed_intent` invocation.

### Impact Explanation
N/A — no state divergence exists. Both sides of the equality (`is_valid_salt` at check time vs. at commit time within the same call) are always equal because there is no intervening mutation path reachable by an unprivileged caller.

### Likelihood Explanation
N/A — the precondition (an unprivileged, single-call mutation of the salt registry) is structurally impossible: salt mutation requires a `Role::DAO`/`Role::SaltManager` held account issuing a separate transaction, which is explicitly out of the attacker's capability per the rules.

### Recommendation
No fix needed. This is confirmed as a dead end / negative result.

### Proof of Concept
A `cargo test` in `contracts/defuse/core` (or `tests/src/tests/defuse/accounts/nonces.rs`) can assert this baseline:
1. Construct a `SaltedNonce` with the current valid salt, sign and call `execute_intents` with an empty `DefuseIntents`.
2. Within the single call, assert success (nonce commits) confirming `is_valid_salt` returned `true` once and stayed `true` through `commit_nonce`.
3. As a control, existing tests already show that salt invalidation via a **separate** privileged transaction *before* the intent call causes `execute_intents` to fail with `"invalid salt"` [9](#0-8)  — demonstrating that only cross-transaction (privileged) changes affect validity, never an in-call mutation from the unprivileged intent payload itself.

No divergence exists; this closes as a negative-result baseline test per the question's own framing.

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L75-77)
```rust
        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;
```

**File:** contracts/defuse/core/src/engine/mod.rs (L86-98)
```rust
    fn verify_intent_nonce(&self, nonce: Nonce, intent_deadline: Timestamp) -> Result<()> {
        let Some(nonce) = VersionedNonce::maybe_from(nonce) else {
            return Ok(());
        };

        match nonce {
            VersionedNonce::V1(SaltedNonce {
                salt,
                nonce: ExpirableNonce { deadline, .. },
            }) => {
                if !self.state.is_valid_salt(salt) {
                    return Err(DefuseError::InvalidSalt);
                }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L102-105)
```rust
    #[inline]
    fn is_valid_salt(&self, salt: Salt) -> bool {
        self.state.is_valid_salt(salt)
    }
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L123-125)
```rust
    fn is_valid_salt(&self, salt: Salt) -> bool {
        self.view.is_valid_salt(salt)
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L95-97)
```rust
    fn is_valid_salt(&self, salt: Salt) -> bool {
        self.salts.is_valid(salt)
    }
```

**File:** contracts/defuse/src/contract/state/salt_registry.rs (L79-82)
```rust
    #[inline]
    pub fn is_valid(&self, salt: Salt) -> bool {
        salt == self.current || self.previous.get(&salt).is_some_and(|v| *v)
    }
```

**File:** contracts/defuse/src/contract/salts.rs (L11-50)
```rust
#[near]
impl SaltManager for Contract {
    #[access_control_any(roles(Role::DAO, Role::SaltManager))]
    #[payable]
    fn update_current_salt(&mut self) -> Salt {
        assert_one_yocto();

        self.salts.set_new().unwrap_or_else(|err| err.panic());
        let current = self.salts.current();

        SaltRotationEvent {
            current,
            invalidated: BTreeSet::new(),
        }
        .emit();

        current
    }

    #[access_control_any(roles(Role::DAO, Role::SaltManager))]
    #[payable]
    fn invalidate_salts(&mut self, salts: Vec<Salt>) -> Salt {
        assert_one_yocto();

        // NOTE: omits any errors
        let invalidated = salts
            .into_iter()
            .filter(|s| self.salts.invalidate(*s).is_ok())
            .collect();

        let current = self.salts.current();

        SaltRotationEvent {
            current,
            invalidated,
        }
        .emit();

        current
    }
```

**File:** contracts/defuse/src/contract/intents/mod.rs (L24-65)
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
}
```

**File:** tests/src/tests/defuse/accounts/nonces.rs (L209-232)
```rust
    // nonce can't be committed with invalidated salt
    {
        let current_salt = env.defuse.current_salt().await.unwrap();
        user.defuse_invalidate_salts(env.defuse.contract_id(), [current_salt])
            .await
            .expect("unable to invalidate salt");

        let deadline = current_timestamp + timeout_delta;
        let invalid_salt_nonce = create_random_salted_nonce(current_salt, deadline, &mut rng);

        env.defuse_simulate_and_execute_intents(
            env.defuse.contract_id(),
            [user
                .sign_defuse_message(
                    env.defuse.contract_id(),
                    invalid_salt_nonce,
                    deadline,
                    DefuseIntents { intents: [].into() },
                )
                .await],
        )
        .await
        .assert_err_contains("invalid salt");
    }
```
