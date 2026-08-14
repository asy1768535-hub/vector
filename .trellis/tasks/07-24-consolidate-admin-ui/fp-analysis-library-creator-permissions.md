# First Principles Analysis: Library Creator Permissions

## Axioms

1. A library creator is recorded in `Library.created_by`; this identity is already authoritative data.
2. Runtime library access requires both an active organization membership and a Casbin action grant when organization authorization is enabled.
3. The permission matrix must change the same authorization inputs that runtime access checks, otherwise a successful-looking grant is ineffective.

## Problem Essence

**Core problem:** library creation and platform permission assignment do not establish all runtime authorization prerequisites.

**Success criteria:** a new library creator receives `read`, `insert`, `delete`, and `admin` immediately; a platform grant creates or reactivates the target organization membership; creator grants cannot be revoked through the matrix; organization isolation remains intact.

## Assumptions Challenged

| Assumption | Challenge | Axiom(s) | Verdict |
|---|---|---|---|
| A platform superuser should read every tenant library | Platform administration is not tenant content ownership | 1, 2 | Discard |
| Casbin alone is sufficient | Runtime joins an active membership before checking Casbin | 2, 3 | Discard |
| Membership alone is sufficient | Ordinary members still require action grants | 2 | Discard |
| Creation can defer authorization to the matrix | The creator identity and intended ownership are already known | 1, 3 | Discard |
| A GET endpoint should repair creator permissions | Reads should not hide authorization mutations | 1, 3 | Discard |
| Existing shared library-list authorization should be rewritten | Its broad call graph is unnecessary for fixing the write-side invariant | 2, 3 | Discard |

## Ground Truths

1. `create_library` receives the authenticated creator and the persisted library in one transaction.
2. `/admin/permissions` is restricted to platform superusers but currently writes only Casbin policy.
3. `OrganizationMembership` supports one active or disabled row per organization and user.

## Reasoning Chain

GT1 + A1 -> establish membership and all four grants during library creation.

GT2 + A2 + A3 -> make platform grants ensure the target membership before writing Casbin.

GT3 + A1 -> make the operation idempotent and reactivate an explicitly re-granted membership.

## Conclusion

Use one service operation that atomically establishes the organization membership prerequisite, writes Casbin policy with compensation, and records an audit event. Call it from library creation and the platform permission endpoint. Preserve the platform/tenant boundary by granting only the selected or newly created library.

## Validation

- [x] Every conclusion traces to a ground truth.
- [x] Every ground truth is covered.
- [x] No analysis phase was skipped.
- [x] Inversion stress test: granting every platform superuser implicit tenant access was rejected because it would guarantee organization-boundary leakage.
