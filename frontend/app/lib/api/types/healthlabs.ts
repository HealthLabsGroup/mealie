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
export type HealthLabsRecipeScope = "personal" | "catalogue";

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
export interface HealthLabsDraftIngredient {
  referenceId?: string;
  quantity?: number | null;
  food?: HealthLabsFoodIn | null;
  unit?: HealthLabsUnitIn | null;
  note?: string | null;
  originalText?: string | null;
  title?: string | null;
  confidence?: HealthLabsIngredientConfidence;
}
export interface HealthLabsFoodIn {
  id?: string | null;
  name: string;
  pluralName?: string | null;
  description?: string;
}
export interface HealthLabsUnitIn {
  id?: string | null;
  name: string;
  pluralName?: string | null;
  abbreviation?: string;
  pluralAbbreviation?: string;
}
export interface HealthLabsIngredientConfidence {
  average?: number | null;
  quantity?: number | null;
  unit?: number | null;
  food?: number | null;
  comment?: number | null;
}
export interface HealthLabsFoodOut {
  id: string;
  name: string;
  pluralName?: string | null;
  description?: string;
}
export interface HealthLabsIdentity {
  externalSubjectId: string;
  userId: string;
}
export interface HealthLabsImportDraftOut {
  id: string;
  state: string;
  recipe: HealthLabsImportRecipeDraft;
  createdAt: string;
  expiresAt: string;
}
export interface HealthLabsImportRecipeDraft {
  name: string;
  description?: string | null;
  scope?: HealthLabsRecipeScope | null;
  servings?: number;
  yieldQuantity?: number;
  yieldName?: string | null;
  totalTimeSeconds?: number | null;
  prepTimeSeconds?: number | null;
  performTimeSeconds?: number | null;
  sourceUrl?: string | null;
  ingredients?: HealthLabsDraftIngredient[];
  instructions?: HealthLabsInstruction[];
  authoring?: {
    [k: string]: boolean;
  };
}
export interface HealthLabsInstruction {
  title?: string | null;
  summary?: string | null;
  text: string;
  ingredientReferences?: string[];
}
export interface HealthLabsImportDraftRequest {
  url: string;
}
export interface HealthLabsIngredientIn {
  referenceId?: string;
  quantity?: number | null;
  food?: HealthLabsFoodIn | null;
  unit?: HealthLabsUnitIn | null;
  note?: string | null;
  originalText?: string | null;
  title?: string | null;
}
export interface HealthLabsIngredientOut {
  referenceId: string;
  quantity?: number | null;
  food?: HealthLabsFoodOut | null;
  unit?: HealthLabsUnitOut | null;
  note?: string | null;
  originalText?: string | null;
  title?: string | null;
}
export interface HealthLabsUnitOut {
  id: string;
  name: string;
  pluralName?: string | null;
  abbreviation?: string;
  pluralAbbreviation?: string;
}
export interface HealthLabsOperationOut {
  idempotencyKey: string;
  state: string;
  requestSha256: string;
  responseStatus?: number | null;
  responseBody?: {
    [k: string]: unknown;
  } | null;
  completedAt?: string | null;
  expiresAt: string;
}
export interface HealthLabsRecipeOut {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  scope: HealthLabsRecipeScope;
  revisionId?: string | null;
  revisionNumber: number;
  copiedFromRecipeId?: string | null;
  copiedFromRevisionId?: string | null;
  copiedAt?: string | null;
  servings?: number;
  yieldQuantity?: number;
  yieldName?: string | null;
  totalTimeSeconds?: number | null;
  prepTimeSeconds?: number | null;
  performTimeSeconds?: number | null;
  sourceUrl?: string | null;
  image?: string | null;
  ingredients?: HealthLabsIngredientOut[];
  instructions?: HealthLabsInstruction[];
  authoring?: {
    [k: string]: boolean;
  };
}
export interface HealthLabsRecipePage {
  items: HealthLabsRecipeOut[];
  nextCursor?: string | null;
}
export interface HealthLabsRecipeRevisionOut {
  id: string;
  recipeId: string;
  revisionNumber: number;
  contentSha256: string;
  createdAt: string;
  recipe: HealthLabsRecipeOut;
}
export interface HealthLabsRecipeWrite {
  name: string;
  description?: string | null;
  scope?: HealthLabsRecipeScope | null;
  servings?: number;
  yieldQuantity?: number;
  yieldName?: string | null;
  totalTimeSeconds?: number | null;
  prepTimeSeconds?: number | null;
  performTimeSeconds?: number | null;
  sourceUrl?: string | null;
  ingredients?: HealthLabsIngredientIn[];
  instructions?: HealthLabsInstruction[];
  authoring?: {
    [k: string]: boolean;
  };
}
