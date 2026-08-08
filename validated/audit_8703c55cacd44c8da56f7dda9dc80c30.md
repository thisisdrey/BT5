### Title
Priority-floor QoS gate uses unverified, self-declared fee data pre-signature-check, letting unstaked attackers evade load-shedding and force full ed25519 verification - ([File: core/src/sigverify.rs])

### Summary
`SigVerifyWorkerPool::run_transaction_task` applies the scheduler-published priority floor via `apply_priority_floor_to_batch`, which calls `calculate_priority_from_bytes` (`core/src/transaction_priority.rs:73-88`) *before* `sigverify::ed25519_verify_serial` runs. That priority is derived entirely from unverified, attacker-controlled packet bytes (declared `compute_unit_price`/signature count), with no signature validity or fee-payer balance check. An unstaked attacker can therefore forge an arbitrarily high priority for free and always survive the floor, defeating the very load-shedding mechanism meant to protect sigverify CPU under saturation.

### Finding Description
- `run_transaction_task` (`core/src/sigverify.rs:266-324`) calls `apply_priority_floor_to_batch(&mut batch, floor, &working_bank)` (`core/src/sigverify.rs:413-440`) **before** `sigverify::ed25519_verify_serial(&mut batch, ...)` (`core/src/sigverify.rs:326-331`).
- `apply_priority_floor_to_batch` computes priority via `calculate_priority_from_bytes` (`core/src/transaction_priority.rs:73-88`), which only parses the packet as a `SanitizedTransactionView`/`RuntimeTransaction` (structural sanitization) and reads its `transaction_configuration` (declared `compute_unit_price`, `compute_unit_limit`, signature counts) — it never calls signature verification and never checks fee-payer balance.
- `calculate_priority_and_cost` (`core/src/transaction_priority.rs:32-66`) computes `priority = reward * MULTIPLIER / (cost + 1)`, where `reward` comes from `solana_fee::calculate_fee_details` (`fee/src/lib.rs:29-39`) using only the declared `priority_fee_lamports`/`lamports_per_signature * signature_count` — both attacker-controlled, unauthenticated fields — and `cost` is a cost-model estimate over declared compute-unit-limit/instructions, also attacker-controlled.
- Consequently, an unstaked attacker can craft a structurally valid but garbage-signed transaction (invalid signatures, non-existent/unfunded fee payer) with a maximal declared `compute_unit_price` and minimal instruction/CU footprint, producing an artificially inflated `priority` value with zero real payment or verification cost. Such a packet will never be `<= floor`, so `any_kept = true` and it is never marked `discard()`; it proceeds unconditionally into the expensive `ed25519_verify_serial` pass, exactly the class of packet the floor is meant to shed under saturation.
- The comment in `apply_priority_floor_to_batch` ("Unparseable packets are kept and left for downstream rejection") shows the floor already deliberately admits some ambiguous cases; this exploit shows even *parseable* garbage can always defeat it by lying about fee fields, since nothing at this stage checks signature validity or fee-payer solvency.

### Impact Explanation
This is a QoS-evasion bug: the priority floor (`SchedulerPriorityFloor`, `banking-stage-ingress-types/src/lib.rs:71-95`, wired via `SigVerifyWorkerState::priority_floor`, `core/src/sigverify.rs:57-68`) exists specifically to shed low-value packets *before* the CPU-expensive `ed25519_verify_serial` pass once the banking-stage buffer is saturated (`SaturationState`, `core/src/banking_stage/transaction_scheduler/scheduler_controller.rs:39-115`). Because the floor's priority computation trusts unverified, self-reported fee fields, any unstaked attacker can costlessly forge maximal priority and bypass the shed, forcing the leader to spend full signature-verification CPU on garbage precisely when the system is under load and the floor is supposed to be protecting it. This matches the "QoS evasion" / "grossly underpriced pre-fee work" bounty category: work (ed25519 verification) is spent on packets whose declared value is never validated or collected.

### Likelihood Explanation
- Precondition: unstaked TPU delivery only, which is explicitly allowed. However, unstaked TPU traffic is capped by existing QUIC/stream throttling (`streamer/src/nonblocking/stream_throttle.rs`: `MAX_UNSTAKED_TPS = 200`, global unstaked stream-throttling window shared across all unstaked connections, plus per-IP/per-peer connection caps in `streamer/src/nonblocking/swqos.rs`). This substantially bounds the achievable volume of attacker packets (≈200 pps aggregate across all unstaked senders), which limits the absolute magnitude of wasted verification work, even though the underlying priority-floor logic is trivially bypassable.
- No special privileges, staking, or leaked keys are required — only a structurally valid transaction (parseable message/config) with a forged high `compute_unit_price`, reachable purely by sending crafted UDP/QUIC TPU packets.
- The bug is deterministic and reproducible via a unit test comparing `calculate_priority_from_bytes` on a garbage-signed, high-declared-fee transaction vs. a genuinely-signed, lower-declared-fee transaction.

### Recommendation
Do not let the pre-verification priority floor be driven purely by unauthenticated, self-declared fee/compute-budget fields. Options:
- Require a lightweight, cheap-but-real check before granting priority credit at this stage (e.g., verify at least one signature format/length sanity, or defer priority-based floor decisions until after a minimal correctness gate), or
- Treat `calculate_priority_from_bytes` output as a heuristic bound only, and cap the effective priority credit that can be granted to packets with unverified signatures (e.g., don't allow more than the median floor delta), or
- Rate-limit/dedicate a strict per-connection budget for "kept because priority looked high" packets so that inflated-priority garbage cannot consume more sigverify capacity than its (still-throttled) share of unstaked bandwidth would otherwise get.

### Proof of Concept
```rust
// core/src/transaction_priority.rs (extend existing test module)
#[test]
fn forged_high_fee_beats_legit_lower_fee_pre_verification() {
    // Bank with non-zero lamports_per_signature so reward != 0.
    let (bank, mint) = test_bank_with_lamports_per_signature(5_000);

    // "Legit" transaction: correctly signed, modest compute_unit_price.
    let legit_bytes = make_tx_bytes(&mint, bank.last_blockhash(), 1_000);
    let legit_priority = calculate_priority_from_bytes(&bank, &legit_bytes).unwrap();

    // "Attacker" transaction: garbage/unfunded signer, declares max compute_unit_price.
    // calculate_priority_from_bytes never checks signature validity or fee-payer
    // balance, so this parses fine and yields a much higher priority for free.
    let attacker_keypair = Keypair::new(); // never funded, signature never checked here
    let forged_bytes = make_tx_bytes(&attacker_keypair, bank.last_blockhash(), u64::MAX);
    let forged_priority = calculate_priority_from_bytes(&bank, &forged_bytes).unwrap();

    assert!(
        forged_priority > legit_priority,
        "forged priority {forged_priority} should exceed legit priority {legit_priority} \
         despite no real payment guarantee"
    );

    // Simulate the floor check performed in apply_priority_floor_to_batch:
    // set floor between legit and forged priority.
    let floor = legit_priority; // legit tx would be dropped at-or-below floor
    assert!(legit_priority <= floor, "legit tx would be shed by the floor");
    assert!(forged_priority > floor, "forged garbage survives the floor for free");
}
```
Expected assertions: the forged/garbage-signed packet's computed priority exceeds a genuinely-signed, lower-fee packet's priority, and specifically exceeds a floor value that would otherwise shed the legit packet — demonstrating that `apply_priority_floor_to_batch` (`core/src/sigverify.rs:413-440`) can be bypassed by unverified, self-declared fee data before `ed25519_verify_serial` ever runs.