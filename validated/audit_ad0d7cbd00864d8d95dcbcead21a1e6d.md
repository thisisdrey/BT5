## Analysis Result

I found a plausible analog in java-tron: a global, non-thread-safe cache (`programPrecompileLRUMap`) that is read and written via an unsynchronized check-then-act pattern, structurally similar to the unlocked `qdisc_rtab_list` get/put race in the CVE. However, I was **not able to fully confirm**, within the remaining tool budget, whether the surrounding call path (`Manager.pushTransaction`) executes the non-constant-call branch of `Program.getProgramPrecompile()` concurrently across multiple caller threads without a JVM-wide lock — this is the critical fact needed to prove the race is actually reachable in production, and I could not verify it with certainty.

### Title
Unsynchronized global JUMPDEST-analysis cache (`programPrecompileLRUMap`) mutated via check-then-act on concurrent TVM executions - ([File: actuator/src/main/java/org/tron/core/vm/program/Program.java])

### Summary
`Program.getProgramPrecompile()` uses a single **static**, non-constant-call-only `LRUMap<Key, ProgramPrecompile>` (`programPrecompileLRUMap`) shared by every `Program` instance in the JVM. Access is a classic check-then-act sequence — `containsKey()` → `get()` / `compile()` → `put()` — with no lock, mirroring exactly the pattern the CVE describes for `qdisc_get_rtab()`/`qdisc_put_rtab()` on `qdisc_rtab_list`. [1](#0-0) [2](#0-1) 

### Finding Description
`LRUMap` (Apache Commons Collections) is not thread-safe: it maintains an internal doubly-linked eviction list that is mutated on every `get`/`put`/eviction. If two threads execute `getProgramPrecompile()` for non-constant-call `Program` instances concurrently, the `containsKey()`/`get()`/`put()` sequence can race: two threads can simultaneously insert entries, trigger eviction of the same node from two directions, or read a partially-relinked node, corrupting the internal linked structure. This is directly analogous to the kernel bug's root cause: a shared, unlocked global collection whose insert/evict logic assumes single-threaded (or externally-serialized) access.

The severity in the CVE stems from the invariant "callers hold RTNL" being silently broken by an unlocked code path (`cls_flower` + `TCA_ACT_FLAGS_NO_RTNL`). The comparable invariant here — "non-constant `Program` execution is single-threaded / externally serialized by the block-processing lock" — is not enforced by `Program` or `Key`/`LRUMap` themselves; it depends entirely on callers of `VMActuator.execute()` never running the non-constant path from more than one thread at a time.

### Impact Explanation
If the invariant is violated, corrupting `LRUMap`'s internal linked list can cause an infinite loop or exception inside `get`/`put` on the shared cache, which is invoked from `Program.getProgramPrecompile()` on every JUMPDEST validation (`verifyJumpDest`) for every non-constant contract call. Because this map is process-wide and touches every subsequent contract execution, corruption would propagate to unrelated transactions and could hang or crash the node process — a broader impact than a single failed transaction, matching the CVE's characterization of `qdisc_rtab_list` corruption being "shared system-wide."

### Likelihood Explanation
**This cannot be confirmed as High/Medium with confidence from the code I was able to inspect.** The exploitability hinges entirely on whether `Manager.pushTransaction()` (transaction pre-validation on broadcast) or any other entry point invokes non-constant `VMActuator.execute()` from multiple threads without external serialization. I located `Manager.pushTransaction` but ran out of tool budget before confirming its locking discipline; if it (or block application vs. broadcast pre-check) is unsynchronized/parallel, the race is directly reachable by an unprivileged client submitting concurrent `TriggerSmartContract` broadcasts, satisfying the "unprivileged transaction broadcaster" reachability bar. If `pushTransaction` is fully serialized (e.g., under a manager-wide lock encompassing VM execution), this cache is never touched concurrently and there is no vulnerability.

### Recommendation
Regardless of the current locking discipline, harden `programPrecompileLRUMap` defensively:
- Wrap access to `programPrecompileLRUMap` in `Program.getProgramPrecompile()` with a dedicated lock (or use `Collections.synchronizedMap`), or replace `LRUMap` with a thread-safe LRU implementation (e.g., `Caffeine`/Guava `Cache`, which `TronCache` already wraps elsewhere in the codebase).
- Independently verify and document whether `Manager.pushTransaction()` / non-constant `VMActuator` execution is guaranteed single-threaded; if not, this is a confirmed, directly reachable race requiring the same fix priority as the CVE.

### Proof of Concept
Not constructible with certainty from static analysis alone — a runnable PoC requires confirming that two `VMActuator` executions with `isConstantCall()==false` can be driven concurrently (e.g., via two simultaneous `broadcasttransaction` HTTP/gRPC calls each deploying/calling the same contract bytecode so they share the same `getJumpDestAnalysisCacheKey()`), which I could not verify before running out of tool calls.

**Given the unresolved reachability question, I recommend treating this as a lead for further investigation (start a Devin session to trace `Manager.pushTransaction`'s locking and stress-test concurrent `Program.getProgramPrecompile()` calls) rather than a confirmed, provable vulnerability under the strict validation bar requested.**

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L117-119)
```java
  private static final int lruCacheSize = CommonParameter.getInstance().getSafeLruCacheSize();
  private static final LRUMap<Key, ProgramPrecompile> programPrecompileLRUMap
      = new LRUMap<>(lruCacheSize);
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L208-225)
```java
  public ProgramPrecompile getProgramPrecompile() {
    if (isConstantCall()) {
      if (programPrecompile == null) {
        programPrecompile = ProgramPrecompile.compile(ops);
      }
      return programPrecompile;
    }
    if (programPrecompile == null) {
      Key key = getJumpDestAnalysisCacheKey();
      if (programPrecompileLRUMap.containsKey(key)) {
        programPrecompile = programPrecompileLRUMap.get(key);
      } else {
        programPrecompile = ProgramPrecompile.compile(ops);
        programPrecompileLRUMap.put(key, programPrecompile);
      }
    }
    return programPrecompile;
  }
```
