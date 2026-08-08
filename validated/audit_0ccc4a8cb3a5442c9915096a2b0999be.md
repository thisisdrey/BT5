### Title
Piecewise-quadratic `big_mod_exp_mult_complexity` cost approximation can under-price large modular-exponentiation syscall work - (File: `syscalls/src/lib.rs`)

### Summary
The compute cost charged for the `sol_big_mod_exp` syscall is derived from `big_mod_exp_mult_complexity` and `big_mod_exp_operation_cost`, a piecewise quadratic approximation (SIMD-0529) of the true asymptotic cost of a big-integer modular exponentiation. [1](#0-0)  This is structurally the same bug class as the referenced report: a fitted/approximate cost function that is only accurate near the points it was calibrated on and can diverge meaningfully at the extremes of its input range, and the divergence directly controls how much real computation is charged for versus how much is priced.

### Finding Description
`big_mod_exp_mult_complexity(input_len)` computes an "operation complexity" using three different quadratic-like formulas depending on whether `input_len` (the max of base/modulus length, up to `BIG_MOD_EXP_MAX_BYTES` = 512) falls in `<=64`, `<=1024`, or `>1024` bins: [1](#0-0) 

`big_mod_exp_operation_cost` multiplies this by the adjusted exponent bit-length (or a fixed reduction factor when the exponent is 1) and divides by a configured `cost_divisor` to produce the actual compute units charged, on top of a flat `big_modular_exponentiation_base_cost`: [2](#0-1) 

This cost is charged, then the syscall performs the real modular exponentiation via `big_mod_exp(base, exponent, modulus)`: [3](#0-2) 

This is directly analogous to the reported `exp()` Taylor-series bug: both are piecewise/approximated cost formulas used to stand in for a real, more expensive function, and both are only validated/tuned for a bounded input domain (the report's `x/g` in `[0,6]`; here `input_len` in `[1, 512]` bytes and `exponent_len` similarly bounded). Any unstaked user can submit an arbitrary BPF transaction invoking this syscall with attacker-chosen `base_len`, `modulus_len`, and `exponent` bytes (up to `BIG_MOD_EXP_MAX_BYTES`), so the formula's accuracy at the edges of its domain (near the 64/1024-byte breakpoints, and for the largest permitted lengths up to 512 bytes) directly determines whether real CPU time spent inside `big_mod_exp` is proportionally priced. Because the fit is empirical (constants such as `/4 + 96*x - 3072` and `/16 + 480*x - 199680` are calibrated approximations of a big-integer multiplication complexity curve, mirroring EIP-198/ERC-198-style approximations), it is plausible that certain lengths near breakpoints or near the 512-byte cap are systematically under-priced relative to actual `num-bigint` modpow cost, similar to how the Solidity `exp()` diverges near `x/g ≈ 5-6`.

### Impact Explanation
If the cost model under-prices actual computation for some subset of permitted `(base_len, modulus_len, exponent_len)` combinations, an attacker can repeatedly submit transactions invoking `sol_big_mod_exp` with those parameters to consume disproportionate real CPU time relative to the compute units charged. Since compute units are the leader's/validator's mechanism for bounding per-block execution time and pricing work, systematic under-pricing at this syscall constitutes "grossly underpriced pre-fee work" within banking-stage/SVM execution, degrading block-production throughput without a corresponding fee/compute-unit cost to the attacker.

### Likelihood Explanation
Exploitability requires finding actual `(base_len, modulus_len, exponent_len)` triples where `big_mod_exp`'s real cost (bounded by `num-bigint`'s modpow implementation, itself super-linear in operand size) exceeds what the piecewise-quadratic approximation charges — this needs an attacker to benchmark the real syscall against the charged cost across the parameter space (up to 512 bytes each) to find a favorable divergence, similar to how the original report needed to search for `x/g` regions with maximal Taylor-series error. This is plausible but unverified from static code alone; I could not access the actual runtime cost measurements or the `num-bigint`/`solana-big-mod-exp` crate's complexity to confirm a concrete under-pricing gap exists, only that the pricing function is an admitted approximation over a bounded, user-controlled input domain — the same structural precondition as the referenced finding.

### Recommendation
- Benchmark the true cost of `big_mod_exp` across the full permitted range of `base_len`/`modulus_len`/`exponent_len` (up to `BIG_MOD_EXP_MAX_BYTES`) and compare against `big_mod_exp_operation_cost`'s output to identify any regions of under-pricing, particularly near the 64-byte and 1024-byte breakpoints and near the 512-byte cap.
- If a gap is found, tighten the piecewise complexity function (e.g., add more breakpoints or a stricter upper-bound fit) so the charged cost is always >= worst-case real cost for every length in range, rather than an average/best-fit approximation.
- Add a fuzz/property test that asserts `big_mod_exp_operation_cost(...)` compute-unit charge times a calibrated cost-per-unit-time is >= measured wall-clock cost of `big_mod_exp` for randomized inputs across the full length range.

### Proof of Concept
Not independently reproduced; a full PoC would require running the actual `sol_big_mod_exp` syscall (or the underlying `big_mod_exp` function) with a grid of `base_len`/`modulus_len` values near 64, 1024, and 512-byte boundaries, comparing measured execution time to charged compute units: [4](#0-3) [5](#0-4)

### Citations

**File:** syscalls/src/lib.rs (L2303-2319)
```rust
fn big_mod_exp_mult_complexity(input_len: u64) -> Option<u128> {
    let input_len = input_len as u128;
    let input_len_squared = input_len.checked_mul(input_len)?;
    if input_len <= 64 {
        Some(input_len_squared)
    } else if input_len <= 1024 {
        input_len_squared
            .checked_div(4)?
            .checked_add(96_u128.checked_mul(input_len)?)?
            .checked_sub(3_072)
    } else {
        input_len_squared
            .checked_div(16)?
            .checked_add(480_u128.checked_mul(input_len)?)?
            .checked_sub(199_680)
    }
}
```

**File:** syscalls/src/lib.rs (L2349-2374)
```rust
/// Compute the operation cost of a big integer modular exponentiation, i.e. the
/// cost charged on top of the flat `big_modular_exponentiation_base_cost`.
fn big_mod_exp_operation_cost(
    cost_divisor: u64,
    params: &BigModExpParams,
    exponent: &[u8],
) -> Option<u64> {
    let input_len = params.base_len.max(params.modulus_len);
    let mult_complexity = big_mod_exp_mult_complexity(input_len)?;
    let operation_complexity = if big_mod_exp_is_one_le(exponent) {
        mult_complexity.checked_mul(u128::from(BIG_MOD_EXP_MOD_REDUCTION_COMPLEXITY_FACTOR))?
    } else {
        let adjusted_exponent_length =
            big_mod_exp_adjusted_exponent_length(exponent).max(BIG_MOD_EXP_MIN_EXPONENT_LENGTH);
        mult_complexity.checked_mul(u128::from(adjusted_exponent_length))?
    };
    let divisor = u128::from(cost_divisor);
    if divisor == 0 {
        return None;
    }

    let operation_cost = operation_complexity
        .checked_add(divisor.checked_sub(1)?)?
        .checked_div(divisor)?;
    u64::try_from(operation_cost).ok()
}
```

**File:** syscalls/src/lib.rs (L2376-2439)
```rust
declare_builtin_function!(
    /// Big integer modular exponentiation
    SyscallBigModExp,
    fn rust(
        invoke_context: &mut InvokeContext<'_, '_>,
        params_addr: u64,
        result_addr: u64,
        _arg3: u64,
        _arg4: u64,
        _arg5: u64,
    ) -> Result<u64, Error> {
        let check_aligned = invoke_context.get_check_aligned();

        // Charge the flat base cost of the syscall up front, before doing any
        // translation or work that could fail without being paid for.
        let execution_cost = invoke_context.get_execution_cost();
        let base_cost = execution_cost.big_modular_exponentiation_base_cost;
        let cost_divisor = execution_cost.big_modular_exponentiation_cost_divisor;
        invoke_context.compute_meter.consume_checked(base_cost)?;

        let memory_mapping = invoke_context.memory_contexts.memory_mapping()?;
        let params =
            *translate_type::<BigModExpParams>(memory_mapping, params_addr, check_aligned)?;

        if params.base_len > BIG_MOD_EXP_MAX_BYTES
            || params.exponent_len > BIG_MOD_EXP_MAX_BYTES
            || params.modulus_len > BIG_MOD_EXP_MAX_BYTES
        {
            return Err(SyscallError::InvalidLength.into());
        }

        // Only the exponent (and the lengths in `params`) is needed to compute
        // the operation cost, so translate it and charge before translating the
        // base and modulus.
        let exponent = translate_slice::<u8>(
            memory_mapping,
            params.exponent,
            params.exponent_len,
            check_aligned,
        )?;
        let Some(cost) = big_mod_exp_operation_cost(cost_divisor, &params, exponent) else {
            // The operation cost cannot be represented as a `u64`, so it can
            // never be paid for; drain the remaining budget and fail.
            invoke_context.compute_meter.consume_checked(u64::MAX)?;
            return Err(Box::new(InstructionError::ComputationalBudgetExceeded));
        };
        invoke_context.compute_meter.consume_checked(cost)?;

        let base = translate_slice::<u8>(
            memory_mapping,
            params.base,
            params.base_len,
            check_aligned,
        )?;
        let modulus = translate_slice::<u8>(
            memory_mapping,
            params.modulus,
            params.modulus_len,
            check_aligned,
        )?;

        let Some(value) = big_mod_exp(base, exponent, modulus) else {
            return Err(SyscallError::InvalidAttribute.into());
        };
```

**File:** programs/sbf/rust/big_mod_exp/src/lib.rs (L8-65)
```rust
fn big_mod_exp_test() {
    // Each case is (base, exponent, modulus, expected), given as big-endian hex.
    let test_cases: &[(&str, &str, &str, &str)] = &[
        (
            "1111111111111111111111111111111111111111111111111111111111111111",
            "1111111111111111111111111111111111111111111111111111111111111111",
            "111111111111111111111111111111111111111111111111111111111111110A",
            "0A7074864588D6847F33A168209E516F60005A0CEC3F33AAF70E8002FE964BCD",
        ),
        (
            "2222222222222222222222222222222222222222222222222222222222222222",
            "2222222222222222222222222222222222222222222222222222222222222222",
            "1111111111111111111111111111111111111111111111111111111111111111",
            "0000000000000000000000000000000000000000000000000000000000000000",
        ),
        (
            "3333333333333333333333333333333333333333333333333333333333333333",
            "3333333333333333333333333333333333333333333333333333333333333333",
            "2222222222222222222222222222222222222222222222222222222222222222",
            "1111111111111111111111111111111111111111111111111111111111111111",
        ),
        (
            "9874231472317432847923174392874918237439287492374932871937289719",
            "0948403985401232889438579475812347232099080051356165126166266222",
            "25532321a214321423124212222224222b242222222222222222222222222444",
            "220ECE1C42624E98AEE7EB86578B2FE5C4855DFFACCB43CCBB708A3AB37F184D",
        ),
        (
            "3494396663463663636363662632666565656456646566786786676786768766",
            "2324324333246536456354655645656616169896565698987033121934984955",
            "0218305479243590485092843590249879879842313131156656565565656566",
            "012F2865E8B9E79B645FCE3A9E04156483AE1F9833F6BFCF86FCA38FC2D5BEF0",
        ),
        (
            "0000000000000000000000000000000000000000000000000000000000000005",
            "0000000000000000000000000000000000000000000000000000000000000002",
            "0000000000000000000000000000000000000000000000000000000000000007",
            "0000000000000000000000000000000000000000000000000000000000000004",
        ),
        (
            "0000000000000000000000000000000000000000000000000000000000000019",
            "0000000000000000000000000000000000000000000000000000000000000019",
            "0000000000000000000000000000000000000000000000000000000000000064",
            "0000000000000000000000000000000000000000000000000000000000000019",
        ),
        (
            "0000000000000000000000000000000000000000000000000000000000000019",
            "0000000000000000000000000000000000000000000000000000000000000019",
            "0000000000000000000000000000000000000000000000000000000000000000",
            "0000000000000000000000000000000000000000000000000000000000000000",
        ),
        (
            "0000000000000000000000000000000000000000000000000000000000000019",
            "0000000000000000000000000000000000000000000000000000000000000019",
            "0000000000000000000000000000000000000000000000000000000000000001",
            "0000000000000000000000000000000000000000000000000000000000000000",
        ),
    ];
```
