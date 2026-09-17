Based on the analysis, there's a valid analog. The GnuTLS CVE-2024-0567 bug class is about a trust-validation logic flaw when a chain relies on multiple independent signing entities ("distributed trust") — the validator's aggregation/deduplication logic mishandles equivalent-but-differently-encoded proof material. The closest reachable analog in java-tron is the signature-deduplication logic inside the `ValidateMultiSign` TVM precompile, which aggregates weighted signatures from a distributed set of keys but only de-duplicates by exact signature bytes, not by recovered signer address.

### Title
Weight double-counting in TVM `ValidateMultiSign` precompile allows multi-sig threshold bypass via signature malleability - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract aggregates the weight of each recovered signer address to compare against a `Permission` threshold. Its de-duplication guard is intended to prevent a single key from being counted twice, but the guard only skips accumulation when the *exact byte-identical* signature repeats; if the same address recovers from two distinct (but validly malleable) signature encodings, the loop falls through and adds the same key's weight again.

### Finding Description
In `PrecompiledContracts.ValidateMultiSign.execute()`: [1](#0-0) 

For each provided signature, `recoveredAddr` is computed, and `sign` is set to `merge(recoveredAddr, sign)`. The check `ByteArray.matrixContains(executedSignList, recoveredAddr)` detects that this address already contributed weight; the inner check `ByteArray.matrixContains(executedSignList, sign)` only `continue`s (skips) if the *exact same signature bytes* were seen before. If the caller instead supplies a second, byte-different signature that still recovers to the same address (e.g. an ECDSA malleable variant, or simply a different valid signature produced by re-signing the same hash with the same private key — ECDSA is not deterministic unless RFC6979 is enforced, and no low-S/canonical-form check is visible in this path), execution does **not** `continue`; it proceeds to `totalWeight += weight` again for the same key, and re-adds the address/signature pair to `executedSignList` for future iterations.

This means a caller holding a single private key (single weight unit) can synthesize N distinct valid signatures over the same `hash` and have the precompile report a cumulative weight of N × weight, without any additional distinct keys — defeating the entire purpose of the weighted multi-signature threshold that the `Permission`/`Key` model (`chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java:218-226`, threshold check in `AccountPermissionUpdateActuator.checkPermission`) is designed to enforce for "distributed trust" (multiple independent signers) scenarios.

### Impact Explanation
Any smart contract on TRON that uses `ValidateMultiSign` (address `0x66`) as an on-chain authorization gate for value-moving operations (custom multi-sig wallets, escrow, DAO treasuries, bridges) can be defrauded: an attacker controlling only one authorized key with insufficient individual weight can still satisfy the permission threshold and trigger a `true` (`dataOne()`) result, leading to unauthorized approval of a transfer or contract action — i.e., concrete unauthorized account operation / theft of funds gated by that contract's multi-sig check. This is reachable by any contract deployer/caller (`TriggerSmartContract`), matching the "unprivileged" reachability requirement.

### Likelihood Explanation
High likelihood of reachability: `ValidateMultiSign` is a standard, documented precompile callable from any deployed contract via a normal `TriggerSmartContract`. Constructing a second valid ECDSA signature over the same hash from the same private key requires no privileged access — either through non-deterministic nonce reuse or classic (s, 65-n·s) malleable transforms; the precompile does not enforce canonical low-S encoding before recovery, so at least the trivial "resign with a different nonce" path is always available to the key holder.

### Recommendation
Change the de-duplication key in the loop to be based solely on `recoveredAddr` (not on the raw `sign` bytes): once an address has contributed weight, any subsequent signature recovering to that same address must be skipped entirely (`continue`), regardless of whether the signature bytes match exactly. Additionally, consider enforcing canonical (low-S) signature encoding before calling `recoverAddrBySign` to remove trivial malleability.

### Proof of Concept
1. Deploy/set up an account with an `Active` permission containing key `K` at weight `w1` and threshold `T`, where `T > w1` (so `K` alone is insufficient).
2. From a contract, call precompile `0x66` (`validatemultisign`) with `signatures = [sig1, sig2]`, where `sig1` and `sig2` are two distinct valid ECDSA signatures produced by `K` over the same `hash` (e.g. two separate `key.sign(hash)` calls, which by default produce different `r/s`/malleable pairs since standard `ECKey.sign` is not forced into RFC6979-only single-shot determinism in this codebase's signing utility, or by applying the classic `(r, n-s, flip-recId)` malleable transform to one signature).
3. Both `sig1` and `sig2` recover to the same address `K`, but `matrixContains(executedSignList, sign)` fails for the second because `sign` (post-`merge`) differs; the loop adds `weight` twice, so `totalWeight = 2*w1 ≥ T`, and the precompile returns `(true, dataOne())` even though only one authorized key actually signed. [1](#0-0)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1088-1106)
```java
            for (byte[] sign : signatures) {
              byte[] recoveredAddr = recoverAddrBySign(sign, hash);

              sign = merge(recoveredAddr, sign);
              if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
                if (ByteArray.matrixContains(executedSignList, sign)) {
                  continue;
                }
                MUtil.checkCPUTime();
              }
              long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
              if (weight == 0) {
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }
```
