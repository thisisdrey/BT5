Confirmed: `GetTransactionSignWeightServlet.doPost` [1](#0-0)  is an unauthenticated HTTP endpoint that directly forwards any client-supplied transaction into `TransactionUtil.getTransactionSignWeight`, with no signature or account ownership requirement to call it.

### Title
Timing side-channel in `GetTransactionSignWeight` API leaks account/permission existence and multi-sig key structure via early-exit vs. full signature-recovery path - (File: `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`)

### Summary
The `wallet/getsignweight` HTTP endpoint (backed by `TransactionUtil.getTransactionSignWeight`) and its gRPC/`Wallet` equivalents (`Wallet.getTransactionApprovedList` / `TransactionCapsule.validateSignature`) validate account and permission existence *before* performing any cryptographic signature verification. When the target account or `permissionId` does not exist, the call throws a `PermissionException` almost immediately. When the account and permission *do* exist, the code proceeds to `TransactionCapsule.checkWeight`, which performs an ECDSA public-key recovery (`SignUtils.signatureToAddress`) for every signature supplied in the request. This creates a measurable, attacker-observable timing discrepancy analogous to the SWS Basic-Auth bug: "invalid username" (nonexistent account/permission) returns fast, "valid username" (existing account/permission) takes measurably longer because of the expensive per-signature elliptic-curve recovery loop.

### Finding Description
`TransactionUtil.getTransactionSignWeight` [2](#0-1)  first fetches the account by owner address and returns immediately with a `PermissionException` ("Account does not exist!" / "Permission for this, does not exist!") if the account or permission is missing. Only if both exist does it call `TransactionCapsule.checkWeight`, which loops over every supplied signature and calls `SignUtils.signatureToAddress` (ECDSA recovery) for each one [3](#0-2) . The number of expensive recovery operations executed is directly proportional to the number of signatures the caller supplies and to whether the target permission (and its key list/threshold) actually exists, so a caller can:
1. Submit a transaction naming a target owner address with 0 signatures and observe whether the fast "Account does not exist" / "Permission ... does not exist" path is taken vs. the slower success/`NOT_ENOUGH_PERMISSION` path — enumerating which addresses have accounts and which `permissionId`s (0, 2, or custom active permission IDs configured via `AccountPermissionUpdateActuator`) are valid for that address.
2. Vary the number of garbage signatures supplied (up to `getTotalSignNum()`) and measure the response time, since each additional signature costs one more EC recovery in `checkWeight` before the loop exits — this can be used to infer whether the permission's key count/threshold structure differs between accounts, revealing multisig configuration details that are otherwise not directly queryable.

This is reachable by any anonymous HTTP/gRPC client with no authentication, since `GetTransactionSignWeightServlet` performs no auth and simply forwards attacker-controlled input [1](#0-0) . The identical early-exit-vs-expensive-path pattern also exists in `TransactionCapsule.validateSignature`/`validatePubSignature`, used during transaction broadcast validation [4](#0-3) [5](#0-4) .

### Impact Explanation
The impact is limited to information disclosure (CWE-204/CWE-208 class), consistent with the CVSS `C:L/I:N/A:N` rating of the analog SWS advisory. An attacker can enumerate valid TRON accounts, valid custom `permissionId`s on those accounts, and coarsely infer multisig key-count/threshold structure for a target address without needing any valid credentials. This information can be used to focus further targeted attacks (e.g., social engineering, targeted key-compromise attempts, or crafting malicious multisig approval requests) against accounts confirmed to have particular permission configurations. It does not by itself allow theft of funds, unauthorized transactions, or node compromise.

### Likelihood Explanation
Likelihood is moderate: the endpoint is reachable by anyone with HTTP/gRPC access to a FullNode (no signature or balance required to call `getTransactionSignWeight`/`getTransactionApprovedList`), and the timing gap is deterministic (driven by loop count of EC point recovery operations), making it statistically distinguishable over repeated samples exactly as demonstrated in the SWS PoC methodology. However, exploitation requires network-level timing measurement capability and repeated sampling to overcome jitter/noise, which raises the bar somewhat versus a purely binary signal.

### Recommendation
- Perform account/permission-existence checks and cryptographic signature verification in constant time relative to each other, e.g., by always executing a fixed number of dummy EC recovery operations even when the account/permission does not exist, or by deferring the existence check until after signature processing.
- Alternatively, rate-limit and require the caller to already hold a plausible signed transaction (reject unsigned/probe-only requests) before doing any account lookups, reducing the value of this endpoint as a probing oracle.
- Consider making `wallet/getsignweight` and `wallet/getapprovedlist` require the same authentication/rate-limiting posture as other privileged introspection APIs, since they leak account/permission-configuration details.

### Proof of Concept
1. Pick a target address `A` known/unknown to exist on-chain.
2. POST to `/wallet/getsignweight` with a `Transaction` whose `raw_data.contract[0]` owner address is `A`, `permission_id = 0`, and zero signatures. Measure response time `t0`.
3. Repeat with a `permission_id` unlikely to exist (e.g., `99`) for the same address; compare timings — the "account does not exist"/"permission does not exist" fast-path vs. proceeding into `checkWeight` (even with 0 signatures, cheaper, but scaling with supplied signature count) produces a measurable difference when signatures are added, as more EC-recovery iterations execute in `checkWeight` for existing accounts/permissions.
4. Repeat step 2/3 with N forged signatures (increasing `N` up to `getTotalSignNum()`), observing the linear timing increase for existing accounts vs. the flat, fast rejection for nonexistent accounts/permissions, statistically confirming the enumeration oracle across repeated trials (mirroring the 100-iteration averaging methodology in the original SWS report).

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

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L224-253)
```java
      try {
        Contract contract = trx.getRawData().getContract(0);
        byte[] owner = TransactionCapsule.getOwner(contract);
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
        if (Objects.isNull(account)) {
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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-256)
```java
  public static long checkWeight(Permission permission, List<ByteString> sigs, byte[] hash,
      List<ByteString> approveList)
      throws SignatureException, PermissionException, SignatureFormatException {
    long currentWeight = 0;
    if (sigs.size() > permission.getKeysCount()) {
      throw new PermissionException(
          "Signature count is " + (sigs.size()) + " more than key counts of permission : "
              + permission.getKeysCount());
    }
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-496)
```java
  public static boolean validateSignature(Transaction transaction,
      byte[] hash, AccountStore accountStore, DynamicPropertiesStore dynamicPropertiesStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = transaction.getRawData().getContractList().get(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwner(contract);
    AccountCapsule account = accountStore.get(owner);
    Permission permission = null;
    if (account == null) {
      if (permissionId == 0) {
        permission = AccountCapsule.getDefaultPermission(ByteString.copyFrom(owner));
      }
      if (permissionId == 2) {
        permission = AccountCapsule
            .createDefaultActivePermission(ByteString.copyFrom(owner), dynamicPropertiesStore);
      }
    } else {
      permission = account.getPermissionById(permissionId);
    }
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
    long weight = checkWeight(permission, transaction.getSignatureList(), hash, null);
    if (weight >= permission.getThreshold()) {
      return true;
    }
    return false;
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
