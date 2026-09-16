### Title
ValidateMultiSign Precompile Double-Counts Weight for Repeated Signatures From the Same Key, Bypassing the Multi-Sig Threshold - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` TVM precompiled contract (address `0x...0a`) is meant to let a smart contract verify that a set of signatures collectively meets an account's weighted permission threshold, similar in spirit to a multi-sig authorization check. Its duplicate-detection logic only skips a signature when the *exact byte sequence* (address+signature) has already been counted; it does not prevent a single signer from contributing more than once to `totalWeight` by supplying two different, but both cryptographically valid, signatures over the same hash from the same key. This lets one key holder satisfy a threshold that was designed to require multiple distinct approvers — conceptually the same failure class as the reported "signature is replayable" issue: a single valid authorization is reused/duplicated to produce an effect (crossing an approval threshold) that should require independent, non-reusable approvals.

### Finding Description
`ValidateMultiSign.execute()` iterates over the caller-supplied signature array and accumulates `totalWeight`: [1](#0-0) 

The de-duplication check is:
```java
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
``` [2](#0-1) 

When `recoveredAddr` has already been seen but the exact `sign` bytes differ, the code falls through instead of skipping — it only calls `MUtil.checkCPUTime()` (a CPU/time guard) and then adds the address's weight to `totalWeight` again. Because ECDSA signing uses a random nonce `k`, a single private key can trivially produce many distinct, valid `(r, s)` signatures over the identical `hash` value that is passed into this precompile. Each such signature recovers to the same `recoveredAddr` but is bytewise different, so it is never filtered out, and each one adds `weight` to `totalWeight` again.

The `hash` verified is derived purely from caller-controlled `address`, `permissionId`, and `data` — there is no session/context nonce binding each signature to a single-use slot the way the report's `_validateNonce` would in the ERC-4337 analog: [3](#0-2) 

### Impact Explanation
Any smart contract on TRON can invoke `validatemultisign(address, permissionId, data, bytes[] signatures)` to gate a privileged action (e.g., releasing funds, authorizing a withdrawal, approving a governance action) behind an account's weighted "Active" permission threshold. Because a single key holder can inflate `totalWeight` by resubmitting the same message with several distinct signatures, any dApp/contract that relies on this precompile's boolean result to enforce a genuine N-of-M multi-party approval can be tricked into approving an action with only one real approver. This is a concrete unauthorized-account-operation bug: it defeats the intended access-control guarantee (multiple independent signers) of a user- and dApp-facing on-chain primitive.

### Likelihood Explanation
Reaching this code requires only deploying/calling a contract that invokes the `ValidateMultiSign` precompile (enabled when `VMConfig.allowTvmSolidity059()` is active), which any unprivileged transaction broadcaster/contract deployer can do. Producing multiple valid signatures for the same key/hash is standard ECDSA behavior (different random `k` per signature) and requires no special access — the attacker is the account owner exercising their own key twice, or an attacker with access to one signer's key in an assumed N-of-M scheme. No privileged role, leaked key, or network condition is required beyond controlling one signer's key, which is exactly the threat model multi-sig thresholds are meant to defend against.

### Recommendation
Change the duplicate check so that once `recoveredAddr` has been counted once, weight is never added again regardless of whether the raw signature bytes differ, e.g.:
```java
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  continue; // an address's weight can only be counted once, no matter how many signatures it produced
}
```
and drop the exact-signature-bytes sub-check, since it provides no security benefit and is what allows the bypass.

### Proof of Concept
1. Configure an account's `Active` permission with threshold = 2, two keys `A` and `B`, each weight 1.
2. Deploy/call a contract that invokes the `ValidateMultiSign` precompile with `address`, `permissionId`, and some `data`.
3. Using only key `A`, sign the derived `hash` (`sha256(address || permissionId || data)`) twice, obtaining two different valid signatures `sig1` and `sig2` (trivial, since ECDSA signing uses a fresh random nonce each time).
4. Call the precompile with `signatures = [sig1, sig2]` (key `B` never signs anything).
5. In `execute()`, the first iteration recovers `A`, adds weight 1 (`totalWeight = 1`). The second iteration recovers `A` again; `matrixContains(executedSignList, recoveredAddr)` is true, but `matrixContains(executedSignList, sign)` is false (different signature bytes), so it falls through and adds weight 1 again (`totalWeight = 2`), meeting the threshold of 2 with only key `A`'s participation.
6. The precompile returns `dataOne()` (success), and the calling contract proceeds as if two independent signers approved the action. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1060-1064)
```java
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);
```

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
