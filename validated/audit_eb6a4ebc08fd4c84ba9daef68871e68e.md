### Title
User Enumeration via Unauthenticated `GetTransactionSignWeight` / `GetTransactionApprovedList` API Response Content - (File: `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`)

### Summary
The Directus advisory (GHSA-jr94-gj3h-c8rf) describes an unauthenticated endpoint that reveals whether an account exists by leaking a discrepancy (there, timing) that occurs before a protection mechanism is applied. The closest reachable analog in java-tron is not a timing side channel in signature verification (that path, `TransactionCapsule.validatePubSignature`/`validateSignature`, performs the same weight/permission computation regardless of whether the account row exists, only differing in default-permission construction — no measurable oracle was found there) but an explicit content-based oracle in the unauthenticated wallet query API `getTransactionSignWeight` and `getTransactionApprovedList`, both exposed via HTTP (`GetTransactionSignWeightServlet`, `GetTransactionApprovedListServlet`) and gRPC (`RpcApiService`) without any authentication.

### Finding Description
`TransactionUtil.getTransactionSignWeight` and `Wallet.getTransactionApprovedList` accept an arbitrary (even unsigned) transaction from any anonymous caller, extract the owner address from its contract, and look it up in `AccountStore`: [1](#0-0) 
If the address does not correspond to an existing account, the response explicitly returns the message `"Account does not exist!"` wrapped in a `PermissionException`, which is caught and surfaced back to the caller as `response_code.PERMISSION_ERROR` with that exact message: [2](#0-1) 
The identical pattern exists in `Wallet.getTransactionApprovedList`: [3](#0-2) 
Both are exposed to any anonymous client through `GetTransactionSignWeightServlet.doPost`, which requires no authentication and simply forwards the caller-supplied address: [4](#0-3) 
and via the equivalent gRPC/JSON-RPC surfaces registered in `RpcApiService`. An attacker can craft a minimal transaction (no valid signature required) whose owner address is the address under test, submit it to this endpoint, and directly read from the response message whether that address exists as an account on-chain — this is a more direct oracle than the timing side channel in the Directus report, but the same bug class (CWE-203: unauthenticated existence-disclosure via a query endpoint).

### Impact Explanation
This allows an unprivileged, anonymous API client to enumerate which addresses are registered/activated accounts on a java-tron node, without spending any bandwidth/energy or broadcasting anything (transactions are never actually submitted to the chain — this is a pure query API). This information can be used for targeted phishing/social engineering, profiling of active wallets, or as a building block for further account-targeted attacks. However, this does not lead to fund theft, permanent freezing, unauthorized state change, node crash, chain split, or key disclosure by itself — it is purely an information-exposure issue, consistent with the Medium severity (`C:L/I:N/A:N`) of the original CVE-2026-26185.

### Likelihood Explanation
High likelihood of exploitability: the endpoint is unauthenticated, requires only a syntactically valid (unsigned) transaction, and is reachable by any HTTP/gRPC client against a full node. No special privileges, keys, or prior state are required — an attacker only needs to iterate candidate addresses.

### Recommendation
Return a generic/uniform error (or a uniform response) for "account does not exist" vs. other permission-check failures in `TransactionUtil.getTransactionSignWeight` and `Wallet.getTransactionApprovedList`, so that response content and structure do not disclose account existence to unauthenticated callers. Consider gating these diagnostic endpoints behind authentication or rate limiting keyed to prevent bulk address-enumeration scans, and audit other query-only APIs (`Wallet`/`TronJsonRpcImpl`) for similar existence-revealing error messages.

### Proof of Concept
1. Construct an unsigned `Transaction` protobuf with a single contract (e.g., `TransferContract`) whose `owner_address` is the target address to test, and any other minimal valid fields.
2. POST the transaction JSON to `/wallet/getsignweight` (backed by `GetTransactionSignWeightServlet`) or call the equivalent gRPC `GetTransactionSignWeight`.
3. Inspect the JSON `result.message` field: if it contains `"Account does not exist!"`, the tested address is not an activated account; any other result (e.g., permission info or `NOT_ENOUGH_PERMISSION`) confirms the address is an existing account.
4. Repeat for a list of candidate addresses to enumerate which addresses are registered accounts on-chain, all without broadcasting any transaction or spending energy/bandwidth. [5](#0-4) [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L199-276)
```java
  public TransactionSignWeight getTransactionSignWeight(Transaction trx) {
    TransactionSignWeight.Builder tswBuilder = TransactionSignWeight.newBuilder();
    Result.Builder resultBuilder = Result.newBuilder();
    if (trx.getSignatureCount() > chainBaseManager.getDynamicPropertiesStore()
        .getTotalSignNum()) {
      resultBuilder.setCode(Result.response_code.OTHER_ERROR);
      resultBuilder.setMessage("too many signatures");
      tswBuilder.setResult(resultBuilder);
      return tswBuilder.build();
    }

    trx = truncateSignatures(trx);
    TransactionExtention.Builder trxExBuilder = TransactionExtention.newBuilder();
    trxExBuilder.setTransaction(trx);
    trxExBuilder.setTxid(ByteString.copyFrom(Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), trx.getRawData().toByteArray())));
    Return.Builder retBuilder = Return.newBuilder();
    retBuilder.setResult(true).setCode(response_code.SUCCESS);
    trxExBuilder.setResult(retBuilder);
    tswBuilder.setTransaction(trxExBuilder);

    if (trx.getRawData().getContractCount() == 0) {
      resultBuilder.setCode(Result.response_code.OTHER_ERROR);
      resultBuilder.setMessage("Invalid transaction: no valid contract");
    } else {
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
        if (tswBuilder.getCurrentWeight() >= permission.getThreshold()) {
          resultBuilder.setCode(Result.response_code.ENOUGH_PERMISSION);
        } else {
          resultBuilder.setCode(Result.response_code.NOT_ENOUGH_PERMISSION);
        }
      } catch (SignatureFormatException signEx) {
        resultBuilder.setCode(Result.response_code.SIGNATURE_FORMAT_ERROR);
        resultBuilder.setMessage(signEx.getMessage());
      } catch (SignatureException signEx) {
        resultBuilder.setCode(Result.response_code.COMPUTE_ADDRESS_ERROR);
        resultBuilder.setMessage(signEx.getMessage());
      } catch (PermissionException permEx) {
        resultBuilder.setCode(Result.response_code.PERMISSION_ERROR);
        resultBuilder.setMessage(permEx.getMessage());
      } catch (Exception ex) {
        resultBuilder.setCode(Result.response_code.OTHER_ERROR);
        resultBuilder.setMessage(ex.getClass() + " : " + ex.getMessage());
      }
    }

    tswBuilder.setResult(resultBuilder);
    return tswBuilder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L658-668)
```java
      try {
        Contract contract = trx.getRawData().getContract(0);
        byte[] owner = TransactionCapsule.getOwner(contract);
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
        if (account == null) {
          throw new PermissionException("Account does not exist!");
        }
        int permissionId = contract.getPermissionId();
        Permission permission = account.getPermissionById(permissionId);
        if (permission == null) {
          throw new PermissionException("Permission for this, does not exist!");
```

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

**File:** framework/src/main/java/org/tron/core/services/http/GetTransactionApprovedListServlet.java (L1-2)
```java
package org.tron.core.services.http;

```
