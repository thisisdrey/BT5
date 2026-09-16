### Title
Unauthenticated Disclosure of Any Account's Multi-Sig Permission Members via `getTransactionSignWeight` - (File: `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`)

### Summary
The `getsignweight` API (HTTP `/wallet/getsignweight` and gRPC `GetTransactionSignWeight`) accepts an arbitrary, unsigned or partially-signed transaction and returns the full `Permission` object — including every `Key` address and weight configured on the referenced account — for whatever `owner_address` the caller places inside the contract. No check is performed that the caller controls, signed, or has any relationship with that account before its permission structure (its multi-sig "group membership") is disclosed. This mirrors the GitLab IDOR (CVE-2019-15581), where an actor with no legitimate relationship to a private group could enumerate its membership through an approval-related endpoint that failed to check authorization before exposing member data.

### Finding Description
`TransactionUtil.getTransactionSignWeight` extracts the `owner` address from the first contract of the submitted transaction and looks up that account's `Permission` (owner, witness, or one of up to 8 active permissions) directly from the `AccountStore`, without verifying that the requester owns, signed, or is otherwise authorized to view that account: [1](#0-0) 

The permission (containing the complete `keys` list with each signer's address and weight, `threshold`, and `operations` bitmap) is placed directly into the response regardless of whether any valid signature was supplied — `trx.getSignatureCount() > 0` only gates the additional `approveList`/`currentWeight` computation, not the permission disclosure itself: [2](#0-1) 

The HTTP servlet exposes this to any anonymous client by simply packing a transaction from POST params and forwarding it to `transactionUtil.getTransactionSignWeight`: [3](#0-2) 

The equivalent gRPC endpoint has the same behavior: [4](#0-3) 

The sibling endpoint `getTransactionApprovedList` (`Wallet.getTransactionApprovedList`) has the same structural pattern — it resolves any account's `Permission` purely from an attacker-supplied `owner_address` inside an unsigned/loosely-signed contract, with no ownership check — although it returns the resolved *approver addresses* rather than the raw `Permission`: [5](#0-4) 

### Impact Explanation
Any anonymous, unprivileged API client can enumerate the full multi-sig configuration (owner/active/witness permission keys, their weights, and threshold) of *any* account on the network — including accounts they neither own nor are a party to — simply by crafting a syntactically valid but unsigned transaction naming that account as the contract owner. This is a confidentiality violation of account governance/authorization structures (analogous to leaking private-group membership), and knowledge of exact signer sets and thresholds materially aids targeted attacks such as social engineering of specific signers, phishing, or planning collusion/bribery against known key holders of a multi-sig-controlled account, without leaving any trace of a normal query (no auth, no on-chain footprint).

### Likelihood Explanation
High likelihood of exploitation: the endpoint is part of the public wallet HTTP/gRPC surface, requires no signature, no fee, and no prior relationship with the target account — only knowledge of the target address, which is public on-chain data. Constructing the minimal transaction (e.g., a `TransferContract` with `owner_address` set to the victim account and no valid signatures) is trivial and rate-limiting (`RateLimiterServlet`) does not constitute an authorization check.

### Recommendation
Do not return permission member/key details for accounts the caller cannot prove control of. At minimum, either (a) require the transaction to carry at least one signature that validates against the referenced account's permission before disclosing the `Permission` object, or (b) strip the `keys` list (or redact addresses) from the returned `Permission` when insufficient signatures are present, returning only aggregate information (e.g., whether threshold is met) rather than the full signer set.

### Proof of Concept
1. Pick any target account address `T` known to use multi-sig (owner/active permission with multiple keys).
2. POST to `http://<node>/wallet/getsignweight` an unsigned transaction whose contract (e.g., `TransferContract`) sets `owner_address` to `T`, with no `signature` field.
3. The response's `permission` field contains the complete list of `keys` (addresses + weights) and `threshold` configured for `T`'s owner/active permission — disclosed to the caller with zero relation to `T` and zero valid signatures, per `TransactionUtil.getTransactionSignWeight` (`actuator/src/main/java/org/tron/core/utils/TransactionUtil.java:224-253`).

### Citations

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

**File:** framework/src/main/java/org/tron/core/services/http/GetTransactionSignWeightServlet.java (L1-40)
```java
package org.tron.core.services.http;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import org.tron.api.GrpcAPI.TransactionSignWeight;
import org.tron.core.utils.TransactionUtil;
import org.tron.protos.Protocol.Transaction;


@Component
@Slf4j(topic = "API")
public class GetTransactionSignWeightServlet extends RateLimiterServlet {

  @Autowired
  private TransactionUtil transactionUtil;

  protected void doGet(HttpServletRequest request, HttpServletResponse response) {

  }

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
}


```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L1068-1074)
```java
    @Override
    public void getTransactionSignWeight(Transaction req,
        StreamObserver<TransactionSignWeight> responseObserver) {
      TransactionSignWeight tsw = transactionUtil.getTransactionSignWeight(req);
      responseObserver.onNext(tsw);
      responseObserver.onCompleted();
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L658-678)
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
```
