/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type HealthLabsCapability =
  | "recipes:read_own"
  | "recipes:write_own"
  | "recipes:read_all"
  | "plans:read_own"
  | "plans:mark_cooked_own"
  | "plans:write_all"
  | "catalogue:read"
  | "catalogue:write";

export interface HealthLabsAccessAuditOut {
  id: string;
  actorUserId?: string | null;
  subjectExternalId?: string | null;
  action: string;
  reason: string;
  resourceType?: string | null;
  resourceId?: string | null;
  occurredAt: string;
}
export interface HealthLabsCapabilityGrantOut {
  id: string;
  userId: string;
  capability: HealthLabsCapability;
  grantedByUserId?: string | null;
  reason: string;
  grantedAt: string;
  revokedAt?: string | null;
}
export interface HealthLabsIdentity {
  externalSubjectId: string;
  userId: string;
}
