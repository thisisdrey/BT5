### Title
Anonymous account-existence oracle via timing side channel in `getTransactionSignWeight`/`getTransactionApprovedList` - (File: `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`, `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
The public, unauthenticated HTTP/gRPC endpoints `/wallet/getsignweight` and `/wallet/getapprovedlist` (backed by `TransactionUtil.getTransactionSignWeight` and `Wallet.getTransactionApprovedList`) take different, timing-distinguishable code paths depending solely on whether the `owner_address` embedded in a submitted (unsigned or arbitrarily-signed) transaction corresponds to an existing on-chain account. This mirrors the ALPINE-CVE-2016-6210 bug class: a fast-reject path for "unknown identifier" versus a slow, cryptographically expensive path for "known identifier," which an unauthenticated remote client can exploit to enumerate valid identifiers via response-time measurement.

### Finding Description
Both API entry points accept a raw `Transaction` protobuf from any anonymous caller — no valid signature is required to call them (the caller only needs to embed a candidate address as `owner_address` and can attach arbitrary dummy signature bytes up to `getTotalSignNum()`):

- `GetTransactionSignWeightServlet.doPost` → `TransactionUtil.getTransactionSignWeight` [1](#0-0) 
- `GetTransactionApprovedListServlet.doPost` → `Wallet.getTransactionApprovedList` [2](#0-1) 
- Same logic is exposed over gRPC via `RpcApiService` [3](#0-2) 

In `TransactionUtil.getTransactionSignWeight`, if the owner address is not present in `AccountStore`, the code throws `PermissionException("Account does not exist!")` immediately — a cheap `accountStore.get(owner)` lookup and no cryptography: [4](#0-3) 

If the account *does* exist, execution proceeds to permission lookup and, when signatures are present, calls `TransactionCapsule.checkWeight`, which performs ECDSA public-key recovery (`ECKey.signatureToAddress`/`recoverAddrBySign`-equivalent) for every attached signature, up to `dynamicPropertiesStore.getTotalSignNum()` signatures: [5](#0-4) 

The identical pattern exists in `Wallet.getTransactionApprovedList`: [6](#0-5) 

ECDSA signature recovery is a comparatively expensive elliptic-curve operation relative to a single key-value store lookup. Because the number of dummy signatures the attacker attaches is fully attacker-controlled (bounded only by `getTotalSignNum()`, a chain parameter typically in the range of several to dozens), the attacker can amplify the timing gap between the "account exists" branch (N EC recoveries) and the "account does not exist" branch (single map lookup, immediate exception) to make the side channel reliably observable over the network, exactly as in the original OpenSSH BLOWFISH-vs-SHA512 timing gap.

### Impact Explanation
This does not directly cause fund theft, but it lets any anonymous API client turn the node into an oracle to enumerate which TRON addresses are activated accounts on the chain, without needing any valid signature, private key, or on-chain transaction. This is information disclosure that can be used as reconnaissance for further targeted attacks (e.g., confirming an address associated with an exchange, OTC desk, or victim is active before attempting phishing, social engineering, or targeted exploitation of that specific account). Per the scan rules this is scoped as a Medium-severity information-disclosure analog reachable purely through the anonymous JSON-RPC/HTTP/gRPC query surface into `Wallet`/`TransactionUtil`, matching the CVE-2016-6210 class (timing-based user/resource enumeration) rather than a fund-loss or RCE bug.

### Likelihood Explanation
Likelihood is high for the enumeration primitive itself: both endpoints are public, unauthenticated, and require no economic cost (no broadcast, no fee, no valid signature — a garbage 65-byte signature is accepted and simply fails EC recovery cheaply, while a well-formed dummy signature still triggers the expensive recovery path). An attacker only needs network access to the HTTP/gRPC API (default exposed on full nodes) and can batch/average many requests per candidate address to filter network jitter, which is standard practice for timing side-channel exploitation.

### Recommendation
Normalize response time between the "account not found" and "account found" branches, e.g., by always performing an equivalent-cost dummy signature-recovery loop (or skip it entirely and defer weight/permission computation) before branching on account existence, or by adding constant-time padding/delay in `TransactionUtil.getTransactionSignWeight` and `Wallet.getTransactionApprovedList` so the account-existence check does not gate an asymmetric-cost computation. Consider also gating these introspection endpoints behind stricter rate limiting per source IP given their oracle potential.

### Proof of Concept
1. Pick a candidate address `A` believed to possibly be a TRON account.
2. Build an unsigned `Transaction` with any contract type (e.g., `TransferContract`) whose `owner_address` = `A`.
3. Attach `getTotalSignNum()` dummy 65-byte signatures (garbage bytes, valid length/format so they pass format checks and reach EC recovery).
4. POST repeatedly to `/wallet/getsignweight` (or `/wallet/getapprovedlist`), measuring server-side response latency (average over many requests to cancel noise).
5. Compare latency against a control address known not to exist on-chain (fast `PermissionException` path) — a statistically significant latency gap indicates `A` is an existing account, without ever needing `A`'s private key or broadcasting anything.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/GetTransactionSignWeightServlet.java (L24-37)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      Transaction transaction = Util.packTransaction(params.getParams(), params.isVisible());
      TransactionSignWeight reply = transactionUtil.getTransactionSignWeight(transaction);
      if (reply != null) {
        response.getWriter().println(Util.printTransactionSignWeight(reply, params.isVisible()));
      } else {
        response.getWriter().println("{}");
      }
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetTransactionApprovedListServlet.java (L24-37)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      Transaction transaction = Util.packTransaction(params.getParams(), params.isVisible());
      TransactionApprovedList reply = wallet.getTransactionApprovedList(transaction);
      if (reply != null) {
        response.getWriter().println(Util.printTransactionApprovedList(reply, params.isVisible()));
      } else {
        response.getWriter().println("{}");
      }
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L1068-1082)
```java
    @Override
    public void getTransactionSignWeight(Transaction req,
        StreamObserver<TransactionSignWeight> responseObserver) {
      TransactionSignWeight tsw = transactionUtil.getTransactionSignWeight(req);
      responseObserver.onNext(tsw);
      responseObserver.onCompleted();
    }

    @Override
    public void getTransactionApprovedList(Transaction req,
        StreamObserver<TransactionApprovedList> responseObserver) {
      TransactionApprovedList tal = wallet.getTransactionApprovedList(req);
      responseObserver.onNext(tal);
      responseObserver.onCompleted();
    }
```

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L224-230)
```java
      try {
        Contract contract = trx.getRawData().getContract(0);
        byte[] owner = TransactionCapsule.getOwner(contract);
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
        if (Objects.isNull(account)) {
          throw new PermissionException("Account does not exist!");
        }
```

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L231-253)
```java
        int permissionId = contract.getPermissionId();
        Permission permission = account.getPermissionById(permissionId);
        if (permission == null) {
          throw new PermissionException("Permission for this, does not exist!");
        }
        if (permissionId != 0) {
          if (permission.getType() != PermissionType.Active) {
            throw new PermissionException("Permission type is wrong!");
          }
          //check operations
          if (!checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
        }
        tswBuilder.setPermission(permission);
        if (trx.getSignatureCount() > 0) {
          List<ByteString> approveList = new ArrayList<>();
          long currentWeight = TransactionCapsule.checkWeight(permission, trx.getSignatureList(),
              Sha256Hash.hash(CommonParameter.getInstance()
                  .isECKeyCryptoEngine(), trx.getRawData().toByteArray()), approveList);
          tswBuilder.addAllApprovedList(approveList);
          tswBuilder.setCurrentWeight(currentWeight);
        }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L661-686)
```java
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
        if (account == null) {
          throw new PermissionException("Account does not exist!");
        }
        int permissionId = contract.getPermissionId();
        Permission permission = account.getPermissionById(permissionId);
        if (permission == null) {
          throw new PermissionException("Permission for this, does not exist!");
        }
        if (permissionId != 0) {
          if (permission.getType() != PermissionType.Active) {
            throw new PermissionException("Permission type is wrong!");
          }
          //check operations
          if (!WalletUtil.checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
        }

        if (trx.getSignatureCount() > 0) {
          List<ByteString> approveList = new ArrayList<>();
          byte[] hash = Sha256Hash.hash(CommonParameter
              .getInstance().isECKeyCryptoEngine(), trx.getRawData().toByteArray());
          TransactionCapsule.checkWeight(permission, trx.getSignatureList(), hash, approveList);
          tswBuilder.addAllApprovedList(approveList);
        }
```
