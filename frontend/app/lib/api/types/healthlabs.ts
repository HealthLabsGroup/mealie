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
  | "catalogue:write"
  | "nutrition:write";
export type HealthLabsNutritionStatus = "matched" | "review_required" | "unknown";
export type HealthLabsRecipeScope = "personal" | "catalogue";
export type HealthLabsPlanEntryType = "breakfast" | "lunch" | "dinner" | "snack";

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
export interface HealthLabsFoodMatchOut {
  id?: string | null;
  foodId: string;
  nutrientFoodId: string;
  sourceFoodId: string;
  name: string;
  preparationState?: string | null;
  status: HealthLabsNutritionStatus;
  country: string;
  matchMethod: string;
  confidence: number;
  reviewed: boolean;
  foodGroup?: string | null;
  plantId?: string | null;
  allergens?: {
    [k: string]: string;
  } | null;
  diets?: {
    [k: string]: string;
  } | null;
  reviewedByUserId?: string | null;
  reviewedAt?: string | null;
  nutrientsPer100g: HealthLabsNutritionValues;
  portions?: HealthLabsFoodPortionOut[];
  source: HealthLabsNutritionSourceOut;
}
export interface HealthLabsNutritionValues {
  kcal?: number | null;
  proteinG?: number | null;
  fibreG?: number | null;
  carbsG?: number | null;
  fatG?: number | null;
  waterMl?: number | null;
}
export interface HealthLabsFoodPortionOut {
  description: string;
  amount: number;
  unit: string;
  grams: number;
}
export interface HealthLabsNutritionSourceOut {
  provider: string;
  version: string;
  country: string;
  sourceUrl: string;
  licenseUrl: string;
  attribution: string;
}
export interface HealthLabsFoodOut {
  id: string;
  name: string;
  pluralName?: string | null;
  description?: string;
  nutrition?: HealthLabsFoodMatchOut | null;
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
export interface HealthLabsIngredientNutrition {
  status: HealthLabsNutritionStatus;
  resolvedGrams?: number | null;
  nutrients?: HealthLabsNutritionValues;
  complete?: boolean;
  missingReason?: string | null;
  foodMatch?: HealthLabsFoodMatchOut | null;
}
export interface HealthLabsIngredientOut {
  referenceId: string;
  quantity?: number | null;
  food?: HealthLabsFoodOut | null;
  unit?: HealthLabsUnitOut | null;
  note?: string | null;
  originalText?: string | null;
  title?: string | null;
  nutrition?: HealthLabsIngredientNutrition | null;
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
export interface HealthLabsPlanEntryStatusPatch {
  version: number;
  cooked: boolean;
}
export interface HealthLabsPlanEntryWrite {
  id: string;
  date: string;
  entryType: HealthLabsPlanEntryType;
  position: number;
  servings?: number | null;
  recipeId?: string | null;
  recipeRevisionId?: string | null;
  title?: string | null;
  leftoverOfEntryId?: string | null;
}
export interface HealthLabsPlanPublicationEntryOut {
  id: string;
  date: string;
  entryType: HealthLabsPlanEntryType;
  position: number;
  servings?: number | null;
  recipeId?: string | null;
  recipeRevisionId?: string | null;
  title: string;
  leftoverOfEntryId?: string | null;
  recipeSnapshot?: {
    [k: string]: unknown;
  } | null;
  cooked?: boolean;
  cookedAt?: string | null;
  version?: number;
}
export interface HealthLabsPlanPublicationOut {
  id: string;
  publicationNumber: number;
  contentSha256: string;
  from: string;
  days: 3 | 7 | 14;
  publishedAt: string;
  idempotencyKey: string;
  entries: HealthLabsPlanPublicationEntryOut[];
}
export interface HealthLabsPlanPublicationWrite {
  expectedPublicationId?: string | null;
  from: string;
  days: 3 | 7 | 14;
  entries: HealthLabsPlanEntryWrite[];
}
export interface HealthLabsRecipeNutrition {
  totals?: HealthLabsNutritionValues;
  perServing?: HealthLabsNutritionValues;
  complete?: boolean;
  missingIngredientReferences?: string[];
  sourceVersions?: HealthLabsNutritionSourceOut[];
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
  ownerExternalSubjectId?: string | null;
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
  calculatedNutrition?: HealthLabsRecipeNutrition | null;
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
export interface NutrientValue {
  amount?: number | null;
  unit: string;
  sourceName?: string | null;
}
export interface NutritionImportResult {
  sourceReleaseId: string;
  createdRelease: boolean;
  foodsImported: number;
  portionsImported: number;
}
export interface SourceFoodRow {
  sourceFoodId: string;
  name: string;
  preparationState?: string | null;
  nutrientsPer100g: {
    [k: string]: NutrientValue;
  };
  rawNutrients?: {
    [k: string]: unknown;
  };
  portions?: SourcePortion[];
}
export interface SourcePortion {
  description: string;
  amount: number;
  unit: string;
  grams: number;
}
export interface SourceManifest {
  provider: string;
  version: string;
  country: string;
  sourceUrl: string;
  licenseUrl: string;
  attribution: string;
  sha256: string;
}
