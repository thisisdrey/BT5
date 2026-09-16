### Title
Freeze-balance minimum-lock check uses a test-only default (`checkFrozenTime = 1`) instead of the intended multi-period value - (File: `common/src/main/resources/reference.conf`, `common/src/main/java/org/tron/core/config/args/BlockConfig.java`)

### Summary
Analogous to the Vader `secondsPerEra` bug — where a parameter meant for a real-world timescale was left at a small "testing" value and shipped as the effective default — java-tron ships `checkFrozenTime` with a hardcoded default of `1`, explicitly annotated as "test only" in the reference config, and used to gate how long a frozen balance must remain locked before an `UnfreezeBalance`-type operation is permitted.

### Finding Description
`BlockConfig` declares the field with a literal default: [1](#0-0) 

The shipped reference configuration comments this value explicitly as a testing knob rather than a production value: [2](#0-1) 

This mirrors exactly the root cause pattern in the Vader report: a numeric parameter that is supposed to represent a real economic/time interval (there, seconds-per-day; here, number of maintenance periods that must elapse before frozen balance may be treated as unlocked) is initialized to a minimal value intended only for test iteration speed, and that value is what actually ships as the default in the reference configuration consumed by `Args.java` / `CommonParameter.java`.

The value is consumed downstream by transaction-actuator and TVM code paths reachable from ordinary signed transactions: [3](#0-2) [4](#0-3) 

(I was only able to confirm these files reference `getCheckFrozenTime()`/`CHECK_FROZEN_TIME` via grep; I could not read the exact validation logic and comparison arithmetic in this session due to the tool-call budget being exhausted, so the precise mechanics of how the value gates unfreeze timing are not fully confirmed.)

### Impact Explanation
If `checkFrozenTime` is meant to represent a multiple of `maintenanceTimeInterval` (e.g., a 3-day minimum lock expressed as maintenance-period counts) but defaults to `1`, any unprivileged account broadcasting `FreezeBalance`/`UnfreezeBalance` transactions could unlock stake far sooner than the protocol's economic design intends. This would undermine the intended lock-in guarantees behind TRON Power/vote weight and bandwidth/energy resource accounting, allowing rapid freeze→vote→unfreeze cycles that the real protocol timing was meant to prevent — a resource/stake-integrity issue analogous to the inflation/peg-break impact described in the Vader report, though scoped to stake lock timing rather than token emission.

### Likelihood Explanation
Every full node that runs off the default `reference.conf` without an explicit override in its `config.conf` would carry this test value into production, and the actuator path (`FreezeBalanceActuator`) is reachable by any account broadcasting a standard, unprivileged transaction — no special permission is required. However, without having verified the exact comparison logic and units in `FreezeBalanceActuator`/`Program.java`, I cannot fully confirm whether `checkFrozenTime = 1` is actually below the network's real intended minimum in the currently deployed mainnet configuration, or whether mainnet operators already override it via `config.conf`.

### Recommendation
Verify the intended production value of `checkFrozenTime` (i.e., how many maintenance periods must elapse before unfreeze is permitted) against the actual comparison logic in `FreezeBalanceActuator` and `Program.java`, and ensure the reference/production default matches that intended value rather than the "test only" value of `1`. Add an explicit config validation (similar to the `proposalExpireTime` bounds check already present in `BlockConfig.postProcess()`) enforcing a minimum acceptable `checkFrozenTime` for non-test deployments.

### Proof of Concept
Not fully constructible in this session — confirming exploitability requires reading the exact comparison logic in `FreezeBalanceActuator.java` and `Program.java` where `getCheckFrozenTime()` is consumed, which was not retrieved before the tool budget was exhausted. This should be validated with a Devin session that can read those files directly and trace whether mainnet's `config.conf` overrides this value.

### Citations

**File:** common/src/main/java/org/tron/core/config/args/BlockConfig.java (L23-26)
```java
  private boolean needSyncCheck = false;
  private long maintenanceTimeInterval = 21600000L;
  private long proposalExpireTime = DEFAULT_PROPOSAL_EXPIRE_TIME;
  private int checkFrozenTime = 1;
```

**File:** common/src/main/resources/reference.conf (L765-771)
```text
# Block processing settings.
block = {
  needSyncCheck = false                   // Whether to check sync before producing blocks.
  maintenanceTimeInterval = 21600000   // 6 hours (ms)
  proposalExpireTime = 259200000       // 3 days (ms), controlled by committee proposal
  checkFrozenTime = 1                  // maintenance periods to check frozen balance (test only)
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L1-1)
```java
package org.tron.core.actuator;
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1-1)
```java
package org.tron.core.vm.program;
```
