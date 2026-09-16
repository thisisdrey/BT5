### Title
Multi-signature weight double-counting via signature malleability in the `ValidateMultiSign` TVM precompile bypasses permission threshold checks - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (invoked from any TVM contract call, e.g. `TriggerSmartContract`) is meant to verify that a set of ECDSA signatures collectively meets a `Permission`'s weight `threshold`, where each signer should count once. The deduplication logic only treats two signatures for the same recovered address as "the same" if the raw signature bytes are byte-for-byte identical. Because ECDSA over secp256k1 permits at least two distinct valid `(r, s, v)` encodings that recover to the same address (`s` and `n-s` with the flipped recovery id — classic signature malleability), an attacker holding a single private key can submit two syntactically different but semantically identical signatures and have the same key's weight counted twice, satisfying a multi-key permission threshold with fewer distinct keys than the permission actually requires.

### Finding Description
`ValidateMultiSign.execute` iterates the supplied signature array and, for repeats of the same recovered address, only skips accumulating weight when the merged `(recoveredAddr, sign)` byte value is an *exact* match already seen; otherwise it calls a CPU-time guard and falls through to add the weight again: [1](#0-0) 

There is no check that `recoveredAddr` alone has already contributed weight — the loop only prevents re-counting the exact same signature bytes, via `ByteArray.matrixContains(executedSignList, sign)`. Since `s` and `n - s` (with the recovery id flipped accordingly) both recover to the same address for a valid ECDSA signature, two differently-encoded signatures produced from the same private key over the same hash will:
1. Recover to the same `recoveredAddr`.
2. Produce two different `merged` byte sequences (different signature bytes), so the exact-match dedup check fails to skip them.
3. Each contribute `TransactionCapsule.getWeight(permission, recoveredAddr)` to `totalWeight`.

This lets a caller reach `totalWeight >= permission.getThreshold()` using a single actual signer whose weight, when doubled, meets or exceeds a threshold intended to require multiple distinct keys.

The check-weight helper used for real transaction signature verification (`TransactionCapsule.checkWeight`) explicitly guards against this by deduping on the recovered *address*, not on raw signature bytes: [2](#0-1) 
That correct pattern is absent in the TVM precompile path.

### Impact Explanation
`ValidateMultiSign` is a smart-contract-facing precompile used by dApps/wallet contracts to check whether an on-chain "approval" satisfies a multi-signature `Permission` (e.g., for on-chain multisig treasuries, escrow, or access-control logic implemented in Solidity). If any contract logic gates a state-changing action (fund release, withdrawal approval, permission grant) on the boolean result of `ValidateMultiSign`, an attacker who controls only one of several required keys can forge a second, malleable-but-valid signature from that same key and pass a threshold that should require independent approval from multiple distinct key holders. This is a concrete authorization bypass (CWE-863) that can lead to unauthorized approval/execution of privileged contract operations, matching the OpenFGA advisory's bug class of an authorization decision being satisfied by effectively double-counting a single grant.

### Likelihood Explanation
Any unprivileged account can call a smart contract that invokes the `ValidateMultiSign` precompile with attacker-supplied signature bytes — no special privilege, node access, or malicious validator/witness role is required. Generating a malleable ECDSA signature pair from a single known private key is a standard, well-documented operation (flip `s -> n - s`, flip `v`), requiring no cryptanalysis. The only barrier to exploitation is that some downstream contract must rely on `ValidateMultiSign`'s result to gate a privileged action — a legitimate and expected usage pattern for the precompile (its purpose is exactly multisig approval checking for contracts).

### Recommendation
In `ValidateMultiSign.execute`, deduplicate strictly by `recoveredAddr` (not by the combined `sign` bytes) before adding weight, mirroring `TransactionCapsule.checkWeight`'s address-based dedup: once an address has contributed weight, any further signature recovering to that same address — regardless of byte encoding — must be skipped rather than only skipped on an exact byte match. Additionally/alternatively, enforce canonical (low-`S`) signature form during recovery so only one valid encoding exists per signer.

### Proof of Concept
1. Create an account with an `Active` permission of threshold `2`, containing two keys `K1` (weight 1) and `K2` (weight 1) — i.e., a 2-of-2 style permission enforced entirely by `ValidateMultiSign`-consuming contract logic.
2. Attacker controls only `K1`.
3. Attacker computes `sigA = sign(hash, K1)` normally, and derives a second valid encoding `sigB` for the same key over the same `hash` by transforming `s -> n - s` and flipping the recovery id (standard secp256k1 malleability) — both `sigA` and `sigB` recover to `K1`'s address but differ in raw bytes.
4. Attacker calls the contract, passing `signatures = [sigA, sigB]` into `ValidateMultiSign`.
5. In the loop: first iteration adds weight 1 for `K1` and records `(K1, sigA)`; second iteration recovers `K1` again, `matrixContains(executedSignList, K1)` is true but `matrixContains(executedSignList, mergedSigB)` is false (different bytes), so it falls through and adds another weight of 1 — `totalWeight` becomes `2`, satisfying `threshold = 2` with only `K1`'s single key, without `K2`'s participation.
6. The precompile returns `DATA_ONE` (success), and any contract gating a privileged action on this result executes it despite lacking genuine approval from the second required key. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1119)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
            long totalWeight = 0L;
            List<byte[]> executedSignList = new ArrayList<>();
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

            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
          }
        } catch (Throwable t) {
          if (t instanceof OutOfTimeException) {
            throw t;
          }
          logger.info("ValidateMultiSign error:{}", t.getMessage());
        }
      }
      return Pair.of(true, DATA_FALSE);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L257-263)
```java
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
```
