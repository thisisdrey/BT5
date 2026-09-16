### Title
Cross-Chain / Cross-Fork Transaction Replay Due to Missing Chain-Binding Identifier in Signed Transaction Hash - (File: chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java)

### Summary
The reported Beanstalk bug is a signature scheme that authenticates a payload without binding it to a specific chain (`chainId`), letting a valid signature be replayed on any chain sharing the same contract/verifying logic. java-tron's core transaction authentication has the analogous property: the hash that is signed and later checked by `validateSignature`/`validatePubSignature` is derived solely from `Transaction.raw` bytes and is not bound to any chain-specific constant (e.g., genesis block hash or a chain id). Replay protection instead relies entirely on the TAPOS check (`Manager.validateTapos`), which ties a transaction to a specific historical block by number+hash.

### Finding Description
`TransactionCapsule.sign()` and `validatePubSignature()` both operate on `getTransactionId()`, which is a hash of `Transaction.raw` (owner address, ref_block_bytes, ref_block_hash, expiration, contract data, fee_limit, timestamp, etc.) [1](#0-0) [2](#0-1) . None of these fields encode a chain-unique constant such as the network's genesis block id or an explicit chain identifier — only `ref_block_bytes`/`ref_block_hash`, which point at a specific historical block that must exist in the verifying node's own `RecentBlockStore`.

The only mechanism preventing a signed transaction from being valid on more than one chain is `Manager.validateTapos`, which looks up the block at `ref_block_bytes` in the *current* chain's `RecentBlockStore` and compares it against `ref_block_hash` from the transaction [3](#0-2) . This check succeeds as long as the verifying chain happens to contain that same numbered block with the same hash — which is unavoidably true for any two chains that share block history up to a split point (a hard fork, a permanent chain split following consensus failure, or a network deliberately/accidentally cloned from another network's chain state up to some height). `Manager.processTransaction` calls `validateTapos` and then `trxCap.validateSignature(...)` in sequence, with neither step incorporating anything that would distinguish chain A from chain B post-split [4](#0-3) .

Consequently, once two chains diverge (chain split/fork) while still sharing pre-fork block history within the TAPOS lookback window, a single validly-signed transaction (transfer, vote, exchange order, asset issuance, freeze/delegate, etc.) referencing a shared historical block and an unexpired `expiration` timestamp remains fully valid and independently executable on *both* chains, because:
- the signed hash is identical on both chains (same raw bytes → same signature),
- the TAPOS check passes identically on both chains (shared ref block),
- there is no `chainId`/genesis-hash component anywhere in the signed payload to differentiate them.

This is the direct structural analog of the reported bug: an EIP-712-style domain missing `chainId` allows replay; here the transaction "domain" (raw bytes) is missing any chain-binding value, and the only replay guard (TAPOS) is itself chain-state-relative rather than chain-identity-based.

### Impact Explanation
On a chain split (which the original report explicitly calls out as the triggering condition — "This can happen when there is also a fork in the chain"), any pending or historical transaction that both branches still recognize via TAPOS can be broadcast and independently applied on each fork by any third party who observed it once, without any new signature from the account owner. For value-moving contracts (`TransferContract`, `TransferAssetContract`, `ExchangeTransactionContract`, `VoteWitnessContract`, `DelegateResourceContract`, etc.) this means unauthorized duplicate execution of the same authorized action across chains — e.g., an intended single transfer/vote/stake action gets executed twice (once per chain) using one signature, which can result in unintended asset movement, double-voting, or double-spending of resource delegations relative to what the signer intended, and complicates any future re-convergence/rollback handling around the fork.

### Likelihood Explanation
Exploitation requires an actual chain split/fork event where the two chains share TAPOS-relevant block history (a scenario the underlying report itself specifies), rather than routine adversarial action by any peer. This is a structural weakness rather than a trivially always-exploitable bug: it depends on a fork occurring and both sides retaining the referenced block in their recent-block window before the transaction's `expiration`. This narrows applicability compared to Beanstalk's L2-migration case (which is trivially exploitable any time the same contract is deployed on multiple chains), but the root cause — a signature scheme with no chain-identity binding — is identical and would fire deterministically whenever such a split occurs.

### Recommendation
Bind the signed transaction hash (or an added companion field validated at the TAPOS stage) to a chain-unique identifier — e.g., include the genesis block id or an explicit configured chain id in the hash computed in `TransactionCapsule.getTransactionId()`/`getRawHash()`, or add a `chainId`/genesis-hash equality check inside `Manager.validateTapos` (or `validateCommon`) alongside the existing ref-block-hash comparison. This ensures a transaction signed for one chain cannot be validly replayed on a diverged/forked chain even when the two chains share pre-fork block history.

### Proof of Concept
Conceptual PoC (cannot be executed without a live two-node fork environment):
1. Run two full java-tron nodes, A and B, sharing identical chain state up to block N (a real or simulated hard fork point), then let them diverge from block N+1 onward.
2. Before divergence, have an account owner sign a `TransferContract` transaction with `ref_block_bytes`/`ref_block_hash` pointing at block N and `expiration` set generously into the future, but do not broadcast it yet.
3. After divergence, broadcast the same raw signed transaction bytes to node A. `Manager.validateTapos` on A succeeds (block N is in A's `RecentBlockStore`), signature validates, transaction executes and moves funds.
4. Broadcast the identical raw signed transaction bytes to node B. `Manager.validateTapos` on B also succeeds (block N is still in B's `RecentBlockStore` since B forked from the same state), the same signature validates against the same hash, and the transaction executes again, independently moving funds on chain B — with no new signature from the owner and no chain-specific rejection anywhere in the path.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L589-595)
```java
  public void sign(byte[] privateKey) {
    SignInterface cryptoEngine = SignUtils
        .fromPrivate(privateKey, CommonParameter.getInstance().isECKeyCryptoEngine());
    ByteString sig = ByteString.copyFrom(cryptoEngine.Base64toBytes(cryptoEngine
        .signHash(getTransactionId().getBytes())));
    this.transaction = this.transaction.toBuilder().addSignature(sig).build();
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L650-680)
```java
  public boolean validatePubSignature(AccountStore accountStore,
      DynamicPropertiesStore dynamicPropertiesStore)
      throws ValidateSignatureException {
    if (!isVerified) {
      if (this.transaction.getSignatureCount() <= 0
              || this.transaction.getRawData().getContractCount() <= 0) {
        throw new ValidateSignatureException("miss sig or contract");
      }
      if (this.transaction.getSignatureCount() > dynamicPropertiesStore
              .getTotalSignNum()) {
        throw new ValidateSignatureException("too many signatures");
      }

      byte[] hash = getTransactionId().getBytes();

      long startNs = System.nanoTime();
      try {
        if (!validateSignature(this.transaction, hash, accountStore, dynamicPropertiesStore)) {
          isVerified = false;
          throw new ValidateSignatureException("sig error");
        }
      } catch (SignatureException | PermissionException | SignatureFormatException e) {
        isVerified = false;
        throw new ValidateSignatureException(e.getMessage());
      } finally {
        logSlowSigVerify(startNs);
      }
      isVerified = true;
    }
    return true;
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L798-823)
```java
  void validateTapos(TransactionCapsule transactionCapsule) throws TaposException {
    byte[] refBlockHash = transactionCapsule.getInstance()
        .getRawData().getRefBlockHash().toByteArray();
    byte[] refBlockNumBytes = transactionCapsule.getInstance()
        .getRawData().getRefBlockBytes().toByteArray();
    try {
      byte[] blockHash = chainBaseManager.getRecentBlockStore().get(refBlockNumBytes).getData();
      if (!Arrays.equals(blockHash, refBlockHash)) {
        String str = String.format(
            "Tapos failed, different block hash, %s, %s , recent block %s, "
                + "solid block %s head block %s",
            ByteArray.toLong(refBlockNumBytes), Hex.toHexString(refBlockHash),
            Hex.toHexString(blockHash),
            chainBaseManager.getSolidBlockId().getString(),
            chainBaseManager.getHeadBlockId().getString());
        throw new TaposException(str);
      }
    } catch (ItemNotFoundException e) {
      String str = String
          .format("Tapos failed, block not found, ref block %s, %s , solid block %s head block %s",
              ByteArray.toLong(refBlockNumBytes), Hex.toHexString(refBlockHash),
              chainBaseManager.getSolidBlockId().getString(),
              chainBaseManager.getHeadBlockId().getString());
      throw new TaposException(str);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1537-1546)
```java
    validateTapos(trxCap);
    validateCommon(trxCap);

    validateDup(trxCap);

    if (!trxCap.validateSignature(chainBaseManager.getAccountStore(),
        chainBaseManager.getDynamicPropertiesStore())) {
      throw new ValidateSignatureException(
          String.format(" %s transaction signature validate failed", txId));
    }
```
