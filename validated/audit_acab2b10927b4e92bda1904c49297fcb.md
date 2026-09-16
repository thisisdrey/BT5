### Title
Untyped/undomained data signing in the `ValidateMultiSign` TVM precompile enables cross-contract and cross-chain signature reuse - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompile (address `0x0a`), reachable from any smart contract via a normal `TriggerSmartContract` transaction, authorizes arbitrary on-chain actions by checking ECDSA signatures against a TRON account's `Permission` weight threshold. The message that gets signed is built as `SHA256(accountAddress || permissionId || data)`, where `data` is a raw 32-byte value fully controlled by the calling contract [1](#0-0) . This hash has no domain separator: it does not bind the signature to the specific calling contract, to a function/type identifier, or to the chain ID. Any smart contract that reuses the same `(accountAddress, permissionId, data)` triple — accidentally (shared encoding convention) or maliciously (a phishing DApp) — can consume a signature that a user produced for a completely different purpose/contract/chain.

### Finding Description
`ValidateMultiSign.execute` decodes `address`, `permissionId`, and an opaque `data` value from the calling contract's ABI-encoded input, then computes:
```
combine = address || permissionId || data
hash = SHA256(combine)
``` [1](#0-0) 
It then recovers the signer for each supplied signature against this `hash` and sums the weights of matching keys in the target account's `Permission`, returning success if the threshold is met [2](#0-1) . `recoverAddrBySign` performs raw ECDSA recovery over this hash with no further context [3](#0-2) .

This is the same "untyped data signing" root cause described in the referenced report: the signed payload lacks
1. a domain separator binding it to the **calling contract's address** (unlike EIP-712's `verifyingContract`), so the exact same `(account, permissionId, data)` signature is valid for every contract on the chain that happens to call `ValidateMultiSign` with those parameters;
2. the **chain ID**, so the signature is replayable on any other TVM-compatible chain (testnets, sidechains, forks) where the account's permission and address are identical;
3. a **type hash / function selector**, so signatures produced for one authorization scheme (e.g. "withdraw" in DApp A) are indistinguishable from signatures intended for a different scheme (e.g. "approve" in DApp B) if both DApps happen to hash their payload into the same 32-byte `data` value using a common convention (e.g. `keccak256(abi.encode(nonce, amount, to))`).

Because `data` is attacker/contract-supplied and opaque to the precompile, any two unrelated Solidity contracts on TVM that use `ValidateMultiSign` as a generic "verify this Tron account's multisig approval" primitive are implicitly sharing the same signing namespace, with only `accountAddress`/`permissionId` (attributes of the *signer*, not the *verifier/contract*) as differentiators. This mirrors the Rigor `Community.sol`/`Project.sol` bug class exactly, but here the primitive is a chain-level, energy-metered precompile usable by every deployed contract rather than a single project's contract.

### Impact Explanation
An attacker who controls or can trick a user into interacting with one DApp that requests a `ValidateMultiSign`-style signature can replay that signature in a second, unrelated DApp (or a fork/sidechain sharing the same address space) that also calls `ValidateMultiSign` with matching `account`/`permissionId`/`data`, causing the second contract to believe the account's owner authorized an action it never intended. Depending on what the consuming DApp gates behind this check (fund transfers, governance actions, withdrawal approval), this can lead to unauthorized account operations and theft of funds, satisfying the "concrete unauthorized account operation / theft of funds" bar.

### Likelihood Explanation
Exploitation requires two independent contracts (or the same contract deployed on two chains) to derive `data` via the same hashing convention for semantically different actions — a plausible but not universal condition, since Solidity developers commonly reuse patterns like `keccak256(abi.encode(...))` for "authorization payloads." The precompile is reachable by any unprivileged account simply by deploying/calling a contract that invokes address `0x0a`, so the attack surface (any current or future TVM contract using this primitive) is broad, but successful exploitation depends on a collision/phishing setup between two specific consuming contracts rather than a universal, always-exploitable flaw in core consensus code.

### Recommendation
Add a proper domain separator to the message hashed inside `ValidateMultiSign` (and the related `BatchValidateSign`), following an EIP-712-style construction: include the precompile's own fixed type identifier, the `chainId` (e.g. from `DynamicPropertiesStore`), and ideally the calling contract's address (`msg.sender`/`CALLER` inside the VM context) in the hash, e.g. `SHA256(domainTag || chainId || callerContract || accountAddress || permissionId || data)`. Document clearly to Solidity developers that `data` passed into this precompile must already be a fully domain-separated hash (contract address + chain id + function selector) to avoid downstream reuse.

### Proof of Concept
1. Deploy `ContractA`, which calls `ValidateMultiSign(address acct, uint256 permissionId, bytes32 data, bytes[] sigs)` at `0x0a` to authorize `transfer(acct, amount)` where `data = keccak256(abi.encode(nonce, amount, recipient))`.
2. Deploy `ContractB` (unrelated, e.g. a lending protocol) that also calls `ValidateMultiSign` with the same `acct`/`permissionId`, computing its own "approve collateral withdrawal" payload the same way: `data = keccak256(abi.encode(nonce, amount, recipient))`.
3. `acct`'s owner signs `SHA256(acct || permissionId || data)` off-chain believing they are authorizing the transfer in `ContractA`.
4. Attacker submits the identical `(acct, permissionId, data, sigs)` tuple to `ContractB`; `ValidateMultiSign.execute` at `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1051-1120` recomputes the identical `hash`, recovers the same signer, and reports the threshold met, letting the attacker trigger the withdrawal in `ContractB` — a transaction the owner never authorized for that contract.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L371-388)
```java
  private static byte[] recoverAddrBySign(byte[] sign, byte[] hash) {
    byte[] out = null;
    if (ArrayUtils.isEmpty(sign) || sign.length < 65) {
      return new byte[0];
    }
    try {
      Rsv rsv = Rsv.fromSignature(sign);
      SignatureInterface signature = SignUtils.fromComponents(rsv.getR(), rsv.getS(), rsv.getV(),
          CommonParameter.getInstance().isECKeyCryptoEngine());
      if (signature.validateComponents()) {
        out = SignUtils.signatureToAddress(hash, signature,
            CommonParameter.getInstance().isECKeyCryptoEngine());
      }
    } catch (Throwable any) {
      logger.info("ECRecover error", any.getMessage());
    }
    return out;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1064)
```java
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
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
