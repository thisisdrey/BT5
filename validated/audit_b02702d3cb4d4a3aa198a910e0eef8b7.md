### Title
`ValidateMultiSign` precompiled contract double-counts a single signer's weight via ECDSA signature malleability, allowing multi-sig threshold bypass - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract computes a permission's `totalWeight` by iterating over the caller-supplied signature array and adding each recovered signer's weight. The de-duplication logic only skips a signature when the *exact same signature bytes* for an address have already been counted; if the same address is recovered again from a **different but still valid** signature over the same hash, its weight is added a second time instead of being rejected.

### Finding Description
In `execute()`:
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
  ...
  totalWeight += weight;
  executedSignList.add(sign);
  executedSignList.add(recoveredAddr);
}
``` [1](#0-0) 

When `recoveredAddr` has already appeared (from a *different* signature blob), the code only calls `MUtil.checkCPUTime()` and then falls through to add the weight **again** — it never `continue`s to skip the duplicate signer. This is exploitable because ECDSA signatures are malleable: given one valid signature `(r, s, v)` over a hash, anyone (without the private key) can derive a second valid signature `(r, n-s, 1-v)` for the same address and same hash. An attacker who obtains a single valid signature from one key (e.g., leaked or legitimately provided for one purpose) can submit both the original and the malleable-derived variant in the `signatures` array. Both recover to the same address but are treated as two independent approvals, so `totalWeight` is incremented twice for a single key.

This directly parallels the reported bug class in the external report: a voter's "vote" (here, a signer's weight) is not properly deduplicated/recorded once counted, letting one participant's approval be counted multiple times to reach a decision threshold.

Contrast this with the correct pattern in `TransactionCapsule.checkWeight`, used for on-chain multi-sig transaction validation, which deduplicates by *recovered address* (`addMap.containsKey(base64)` → throws `"has signed twice!"`), not by raw signature bytes: [2](#0-1) 
`ValidateMultiSign` deviates from this safe pattern by keying part of its dedup check on the full `sign` byte array instead of solely on `recoveredAddr`.

### Impact Explanation
`ValidateMultiSign` is a public precompiled contract callable by any smart contract (and therefore reachable by any unprivileged EOA that deploys or calls such a contract) via TVM opcode dispatch. It is designed to let Solidity contracts verify a Tron account's multi-signature permission threshold on-chain (e.g., custody/wallet contracts gating fund withdrawal on "N-of-M managers approved"). Because a single signer's weight can be double- (or up-to-5x, bounded by `MAX_SIZE`) counted, a contract relying on `ValidateMultiSign` to enforce an N-of-M threshold can be tricked into believing the threshold is met when it is not — e.g., one weak-weight/compromised key reaching the approval bar meant to require multiple independent managers. Any contract built on top of this precompile to gate a fund-moving action is exposed to unauthorized approval and potential draining of contract-held funds, matching the "malicious manager passes proposal / drains funds" impact class.

### Likelihood Explanation
No special privilege is required: any address can call the precompile through a simple contract call (`address(0x...).call(...)` targeting the `ValidateMultiSign` precompile address), constructing the malleable second signature purely from public-key arithmetic on an already-obtained valid signature (no private key access needed for the second signature). The `MAX_SIZE = 5` cap limits the multiplier to at most 5x weight inflation per call, but that is often more than enough to cross a 2-of-3 or similar low threshold with just one real signer.

### Recommendation
Fix the loop so the dedup check keys strictly on `recoveredAddr` and always `continue`/skip weight accrual once an address has been counted, regardless of whether the specific signature bytes differ:
```java
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  continue; // address already counted, ignore malleable/duplicate signature
}
...
totalWeight += weight;
executedSignList.add(recoveredAddr);
```
Remove the `sign`-bytes-based secondary check entirely, since weight must only ever be attributed once per recovered address, mirroring the safe pattern already used in `TransactionCapsule.checkWeight`.

### Proof of Concept
1. Deploy a contract `Vault` that calls the `ValidateMultiSign` precompile to check whether `totalWeight >= threshold` before releasing funds, using an account whose permission has threshold 2 with two managers, one of whom is the attacker (weight 1) — one signature short of the threshold.
2. Attacker obtains their own single valid signature `sig1 = (r, s, v)` over the required hash (this is fully legitimate — attacker is a real key holder with weight 1).
3. Attacker computes the malleable counterpart `sig2 = (r, n-s, 1-v)`, which recovers to the same address as `sig1` but has different bytes.
4. Attacker calls `ValidateMultiSign(address, permissionId, data, [sig1, sig2])`.
5. In the loop: first iteration adds attacker's weight (1) and records `sign1`/`recoveredAddr`. Second iteration recovers the same `recoveredAddr`, `matrixContains(executedSignList, recoveredAddr)` is true, but `matrixContains(executedSignList, sign2)` is false (different bytes), so it does **not** `continue` — it proceeds to add weight (1) again. `totalWeight` becomes 2, meeting/exceeding the threshold of 2 with only a single real signer.
6. The precompile returns `true` (dataOne), and `Vault` incorrectly authorizes the fund release using only one manager's key. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1082-1110)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L242-263)
```java
    HashMap addMap = new HashMap();
    for (ByteString sig : sigs) {
      if (sig.size() < 65) {
        throw new SignatureFormatException(
            "Signature size is " + sig.size());
      }
      String base64 = TransactionCapsule.getBase64FromByteString(sig);
      byte[] address = SignUtils
          .signatureToAddress(hash, base64, CommonParameter.getInstance().isECKeyCryptoEngine());
      long weight = getWeight(permission, address);
      if (weight == 0) {
        throw new PermissionException(
            ByteArray.toHexString(hash) + " is signed by " + encode58Check(address)
                + " but it is not contained of permission.");
      }
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
```
