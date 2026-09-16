### Title
Unchecked integer overflow in freeze-duration expiry calculation - ([File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java])

### Summary
`FreezeBalanceActuator.execute()` computes the expiration timestamp of a frozen stake by multiplying an attacker-supplied `frozenDuration` with the constant `FROZEN_PERIOD` using plain `long` arithmetic, with no overflow check. This is the same bug class described in the external report (`account_size * num_accounts` overflow in `SliceAllocator`): two operands multiplied without overflow protection, one of which is attacker-controlled, in code compiled without overflow checks.

### Finding Description
In `execute()`:
```java
long duration = freezeBalanceContract.getFrozenDuration() * FROZEN_PERIOD;
...
long expireTime = now + duration;
``` [1](#0-0) 

`getFrozenDuration()` is a raw `long` field taken directly from the `FreezeBalanceContract` protobuf supplied by the transaction broadcaster. The only guard on this value in `validate()` is conditioned on a test-only flag:
```java
boolean needCheckFrozeTime = CommonParameter.getInstance().getCheckFrozenTime() == 1;//for test
if (needCheckFrozeTime && !(frozenDuration >= minFrozenTime && frozenDuration <= maxFrozenTime)) {
  throw new ContractValidateException(...);
}
``` [2](#0-1) 

The equivalent native-contract path used from TVM (`freezebalance` precompile) performs the identical unchecked multiplication:
```java
long expireTime = nowInMs + param.getFrozenDuration() * FROZEN_PERIOD;
``` [3](#0-2) 

This is notable because the sibling code path for asset issuance (`AssetIssueActuator`), which performs the analogous `frozenDays * FROZEN_PERIOD` calculation, was explicitly hardened against this exact overflow with a comment and an `addExact` overflow check:
```java
// make sure FrozenSupply.expireTime not overflow
long frozenPeriod = next.getFrozenDays() * FROZEN_PERIOD;
try {
  StrictMathWrapper.addExact(assetIssueContract.getStartTime(), frozenPeriod);
} catch (ArithmeticException e) {
  throw new ContractValidateException("Start time and frozen days would cause expire time overflow");
}
``` [4](#0-3) 

This shows the project is aware of and has previously remediated this exact overflow class, but `FreezeBalanceActuator`/`FreezeBalanceProcessor` were not updated with the same protection, and their bounds check is gated behind a flag documented as test-only.

### Impact Explanation
If `needCheckFrozeTime` is disabled (as its own comment states, "for test", meaning the intended production behavior may not always enforce bounds, or if `minFrozenTime`/`maxFrozenTime` dynamic parameters are misconfigured/relaxed), an attacker can submit a `FreezeBalanceContract` with an extreme `frozenDuration` such that `frozenDuration * FROZEN_PERIOD` silently overflows a signed 64-bit `long`, wrapping to a small or negative value. This collapses `expireTime` to a point at or before the current block time, defeating the intended stake lock-up period. Frozen TRX (which grants bandwidth/energy resource weight and TRON Power for voting) could then be unfrozen immediately, bypassing the economic lock-up that underlies TRON's staking/voting security model — enabling flash freeze/unfreeze cycles to manipulate resource-weight or vote totals without genuinely committing capital for the required duration.

### Likelihood Explanation
Exploitability depends entirely on whether the `needCheckFrozeTime` gate and the `minFrozenTime`/`maxFrozenTime` dynamic-property bounds are actually enforced on mainnet nodes at all times. I was not able to confirm from the code the default/production value of `CommonParameter.getCheckFrozenTime()`; the inline comment ("for test") suggests it may be intended purely as a test bypass (implying production enforces bounds), which would make this specific overflow unreachable in normal operation. This uncertainty could not be resolved with the tools available in this session.

### Recommendation
Regardless of the current default, harden `FreezeBalanceActuator.execute()` and `FreezeBalanceProcessor.execute()` the same way `AssetIssueActuator` was hardened: compute `frozenDuration * FROZEN_PERIOD` and the subsequent addition to `now` using overflow-checked arithmetic (`StrictMathWrapper.multiplyExact`/`addExact` or `Math.multiplyExact`/`addExact`), and make the `frozenDuration` range validation unconditional (not gated behind a test-only flag) so the invariant is enforced independent of runtime configuration.

### Proof of Concept
1. Craft a `FreezeBalanceContract` transaction with `frozen_balance` = minimal valid amount (≥ 1 TRX) and `frozen_duration` set to a very large value (e.g., `Long.MAX_VALUE / FROZEN_PERIOD + 1`).
2. Broadcast it to a node where `CheckFrozenTime` bound enforcement is bypassed or `maxFrozenTime` is not restrictive enough to prevent the multiplication from exceeding `Long.MAX_VALUE`.
3. `FreezeBalanceActuator.execute()` computes `duration = frozenDuration * FROZEN_PERIOD`, which overflows and wraps to a small/negative `long`.
4. `expireTime = now + duration` becomes ≤ `now`.
5. Immediately submit an `UnfreezeBalanceContract` — the frozen balance is unlocked instantly instead of after the intended lock period, since unfreeze eligibility checks `now >= expireTime`. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L69-75)
```java
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    long duration = freezeBalanceContract.getFrozenDuration() * FROZEN_PERIOD;

    long newBalance = accountCapsule.getBalance() - freezeBalanceContract.getFrozenBalance();

    long frozenBalance = freezeBalanceContract.getFrozenBalance();
    long expireTime = now + duration;
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L203-214)
```java
    long frozenDuration = freezeBalanceContract.getFrozenDuration();
    long minFrozenTime = dynamicStore.getMinFrozenTime();
    long maxFrozenTime = dynamicStore.getMaxFrozenTime();

    boolean needCheckFrozeTime = CommonParameter.getInstance()
        .getCheckFrozenTime() == 1;//for test
    if (needCheckFrozeTime && !(frozenDuration >= minFrozenTime
        && frozenDuration <= maxFrozenTime)) {
      throw new ContractValidateException(
          "frozenDuration must be less than " + maxFrozenTime + " days "
              + "and more than " + minFrozenTime + " days");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L76-77)
```java
    long nowInMs = dynamicStore.getLatestBlockHeaderTimestamp();
    long expireTime = nowInMs + param.getFrozenDuration() * FROZEN_PERIOD;
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L269-278)
```java
      // make sure FrozenSupply.expireTime not overflow
      if (chainBaseManager.getForkController().pass(ForkBlockVersionEnum.VERSION_4_8_1)) {
        long frozenPeriod = next.getFrozenDays() * FROZEN_PERIOD;
        try {
          StrictMathWrapper.addExact(assetIssueContract.getStartTime(), frozenPeriod);
        } catch (ArithmeticException e) {
          throw new ContractValidateException(
              "Start time and frozen days would cause expire time overflow");
        }
      }
```
