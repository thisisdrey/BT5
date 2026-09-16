Based on my investigation, I found a direct analog of the reported reorg vulnerability class in java-tron's TRC10 asset-ID assignment mechanism.

### Title
Sequential, Order-Dependent TRC10 Token ID / Exchange ID Assignment Enables Reorg-Driven ID Reassignment to Wrong Asset - ([File: actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java])

### Summary
Unlike the java-tron smart-contract `CREATE` address, which is derived from the full transaction hash and owner address (content-bound and thus reorg-resistant), the numeric identifiers assigned to TRC10 tokens and Exchanges are simple, block-order-dependent counters stored in `DynamicPropertiesStore`. This reproduces exactly the bug class from the external report: an identifier that should uniquely and durably bind to "the asset/entity I intended to create" instead depends on transaction *ordering*, which can change across a chain reorg.

### Finding Description
`AssetIssueActuator.execute` assigns a new TRC10 token's `id` by reading and incrementing a single global counter: [1](#0-0) 
This `tokenIdNum` has no relationship to the issuing transaction's content (owner address, tx hash, asset name) — it is purely a function of "how many `AssetIssueContract` transactions were previously included in the chain." Contrast this with the deterministic, content-derived contract address computation used elsewhere in the code: [2](#0-1) 

The same pattern repeats for exchanges: `ExchangeCreateActuator` assigns `id` from `dynamicStore.getLatestExchangeNum() + 1`: [3](#0-2) 

If two independent `AssetIssueContract` (or `ExchangeCreateContract`) transactions from different, unrelated accounts are broadcast close together and a chain reorg (fork switch before solidification) changes their relative block order, the numeric ID that ends up bound to "token/exchange A" versus "token/exchange B" can flip — exactly mirroring the report's scenario where `daoA`/`daoB` addresses swap due to nonce/deployment-order dependence.

Downstream, `ParticipateAssetIssueActuator` partially mitigates this for its own flow by cross-checking `toAddress` against `assetIssueCapsule.getOwnerAddress()` before honoring the trade: [4](#0-3) 
However, this identity check is a validation added at the *consumption* site, not a property of the ID itself — any other code path (TVM `transferToken`/`tokenBalance` opcodes, external contracts, or off-chain integrations) that references a TRC10 token or Exchange purely by its numeric ID (as is standard practice, since IDs — not names — are the canonical identifier once `allowSameTokenName` is enabled) has no such protection built into the ID-resolution mechanism. I was unable to locate/confirm an `ExchangeTradeActuator` (or equivalently named trade actuator) within the available index to verify whether an analogous owner/token-pair check exists for exchange trades; this remains unverified due to index/tool limits.

### Impact Explanation
If a reorg reorders two colliding `AssetIssueContract` transactions, a party who recorded "token ID N" off-chain (from the first, pre-reorg execution) and later interacts with "ID N" via a smart contract or TVM `transferToken`/`callTokenValue` call would unknowingly transact against a completely different token/issuer after the reorg. Because there is no owner/content binding to the numeric ID itself, this can direct value transfers, freezes, or trades to unintended parties — a direct funds-misdirection risk, analogous to the reported DAO-membership address confusion.

### Likelihood Explanation
This requires a chain reorg on java-tron (fork switch before block solidification) combined with two colliding, near-simultaneous `AssetIssueContract`/`ExchangeCreateContract` transactions from unrelated senders — reachable by any unprivileged transaction broadcaster with no special privilege, matching the reachable-path constraints in the rules. Reorgs before solidification are a normal (if infrequent) occurrence on PoS/committee-based chains, making this a plausible, not purely theoretical, condition. The `ParticipateAssetIssueActuator` owner check reduces likelihood for that specific flow, keeping overall severity at Medium rather than High/Critical for the paths I could verify.

### Recommendation
Bind TRC10/Exchange numeric IDs to transaction content (e.g., derive or additionally store a content hash including the owner address and raw transaction hash, similar to `WalletUtil.generateContractAddress`) so that any downstream consumer resolving "ID N" can independently verify the ID's provenance, rather than trusting a purely sequential, order-dependent counter. At minimum, ensure every consumer of these IDs (TVM opcodes, trade actuators, market actuators) enforces an owner/content check equivalent to what `ParticipateAssetIssueActuator` already does.

### Proof of Concept
1. Attacker/observer prepares two `AssetIssueContract` transactions, `TX_A` (legitimate project) and `TX_B` (attacker's token), timed to land in adjacent blocks.
2. During a chain reorg, block ordering changes such that `TX_B` is processed before `TX_A` in the finalized chain, causing `dynamicStore.getTokenIdNum()` increments (`actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java:72-76`) to assign the ID a user expected for the legitimate project ("ID N") to the attacker's token instead.
3. A user or smart contract that hard-coded/cached "ID N" pre-reorg (e.g., via a TVM contract using `tokenBalance`/`transferToken` opcodes) subsequently interacts with the attacker's token under ID N, resulting in funds sent to or interactions with the wrong, attacker-controlled asset.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L72-76)
```java
      long tokenIdNum = dynamicStore.getTokenIdNum();
      tokenIdNum++;
      assetIssueCapsule.setId(Long.toString(tokenIdNum));
      assetIssueCapsuleV2.setId(Long.toString(tokenIdNum));
      dynamicStore.saveTokenIdNum(tokenIdNum);
```

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L39-52)
```java
  public static byte[] generateContractAddress(Transaction trx) {

    CreateSmartContract contract = ContractCapsule.getSmartContractFromTransaction(trx);
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    TransactionCapsule trxCap = new TransactionCapsule(trx);
    byte[] txRawDataHash = trxCap.getTransactionId().getBytes();

    byte[] combined = new byte[txRawDataHash.length + ownerAddress.length];
    System.arraycopy(txRawDataHash, 0, combined, 0, txRawDataHash.length);
    System.arraycopy(ownerAddress, 0, combined, txRawDataHash.length, ownerAddress.length);

    return Hash.sha3omit12(combined);

  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L78-79)
```java
      long id = addExact(dynamicStore.getLatestExchangeNum(), 1);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
```

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L171-174)
```java
      if (!Arrays.equals(toAddress, assetIssueCapsule.getOwnerAddress().toByteArray())) {
        throw new ContractValidateException(
            "The asset is not issued by " + ByteArray.toHexString(toAddress));
      }
```
