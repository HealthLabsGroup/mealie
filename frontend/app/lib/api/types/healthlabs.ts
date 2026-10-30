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
export type UserType = "human" | "agent";
export type HealthLabsRecipeScope = "personal" | "catalogue";
export type HealthLabsNutritionWorkKind = "ingredient_structure" | "food_match" | "portion_resolution";
export type HealthLabsNutritionWorkState = "pending" | "running" | "review_required" | "completed" | "failed";
export type HealthLabsNutritionCuratorState = "pending" | "running" | "approved" | "insufficient_evidence" | "failed";
export type HealthLabsPlanEntryType = "breakfast" | "lunch" | "dinner" | "snack";

export interface DensityImportResult {
  sourceReleaseId: string;
  createdRelease: boolean;
  densitiesImported: number;
  portionsLinked: number;
  ambiguousRows: number;
}
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
  autoApplied: boolean;
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
  reviewedBy?: HealthLabsReviewerOut | null;
  appleNote?: string | null;
  nutrientsPer100g: HealthLabsNutritionValues;
  portions?: HealthLabsFoodPortionOut[];
  source: HealthLabsNutritionSourceOut;
}
export interface HealthLabsReviewerOut {
  id: string;
  displayName: string;
  userType: UserType;
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
export interface HealthLabsFoodMatchWrite {
  nutrientFoodId: string;
  country: string;
  confidence?: number;
  foodGroup?: string | null;
  plantId?: string | null;
  allergens?: {
    [k: string]: string;
  } | null;
  diets?: {
    [k: string]: string;
  } | null;
}
export interface HealthLabsFoodNutritionOut {
  id: string;
  name?: string | null;
  pluralName?: string | null;
  description?: string | null;
  status: HealthLabsNutritionStatus;
  nutrition?: HealthLabsFoodMatchOut | null;
}
export interface HealthLabsFoodNutritionPage {
  items: HealthLabsFoodNutritionOut[];
  total: number;
}
export interface HealthLabsFoodOut {
  id: string;
  name: string;
  pluralName?: string | null;
  description?: string;
  nutrition?: HealthLabsFoodMatchOut | null;
}
export interface HealthLabsFoodPortionCreate {
  description: string;
  amount: number;
  unit: string;
  grams: number;
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
export interface HealthLabsNutrientFoodOut {
  id: string;
  sourceFoodId: string;
  name: string;
  preparationState?: string | null;
  nutrientsPer100g: HealthLabsNutritionValues;
  portions?: HealthLabsFoodPortionOut[];
  source: HealthLabsNutritionSourceReleaseOut;
}
export interface HealthLabsNutritionSourceReleaseOut {
  provider: string;
  version: string;
  country: string;
  sourceUrl: string;
  licenseUrl: string;
  attribution: string;
  id: string;
  sha256: string;
  importedAt: string;
}
export interface HealthLabsNutritionWorkOut {
  id: string;
  kind: HealthLabsNutritionWorkKind;
  targetId: string;
  state: HealthLabsNutritionWorkState;
  attemptCount: number;
  result?: {
    [k: string]: unknown;
  } | null;
  lastError?: string | null;
  updatedAt?: string | null;
  curatorState?: HealthLabsNutritionCuratorState | null;
  curatorReasonCode?: string | null;
  curatorAttemptCount?: number | null;
  appleNote?: string | null;
  curatorAgent?: HealthLabsReviewerOut | null;
}
export interface HealthLabsNutritionWorkerStatus {
  counts: {
    [k: string]: number;
  };
  curatorCounts: {
    [k: string]: number;
  };
  providerEnabled: boolean;
  providerBudgetExhausted: boolean;
  lastUpdatedAt?: string | null;
  curatorLastUpdatedAt?: string | null;
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
export interface HealthLabsRecipeMetadataEstimatePayload {
  effectiveServings: number;
  servingsAssumption?: string | null;
  ingredients: MetadataIngredientPlan[];
  generalMetadata: MetadataGeneralPlan;
  modelReview: MetadataModelReview;
  validation: MetadataValidation;
  reuseOrigin?: MetadataReuseOrigin | null;
}
export interface MetadataIngredientPlan {
  referenceId: string;
  effectiveGrams: number;
  quantityAssumption?: string | null;
  sourceFoodId?: string | null;
  portionOrDensityId?: string | null;
  assumedPortionAmount?: number | null;
  nutrientsPer100g?: {
    [k: string]: number | null;
  };
  rationale: string;
  gramsQuality: "recorded" | "source_conversion" | "estimated";
  nutrientQuality: {
    [k: string]: "source_backed" | "estimated" | "unavailable";
  };
  sourceReleaseId?: string | null;
  sourceRecordHash?: string | null;
  methods?: ("assumed_quantity" | "generic_food" | "source_substitution" | "model_estimate")[];
  interpretation?: string | null;
  acceptedSourceFoodId?: string | null;
  acceptedPlantId?: string | null;
  acceptedPlantClassified?: boolean | null;
  acceptedFoodGroup?: string | null;
  acceptedInterpretation?: string | null;
  modelNutrientKeys?: ("kcal" | "protein_g" | "carbs_g" | "fat_g" | "fibre_g" | "water_ml")[];
}
export interface MetadataGeneralPlan {
  totalMinutes?: number | null;
  prepMinutes?: number | null;
  activeMinutes?: number | null;
  effort?: (1 | 2) | null;
  rationales?: {
    [k: string]: string;
  };
  plants?: MetadataPlant[];
  plantIngredientReferences?: string[];
  plantsRationale?: string | null;
}
export interface MetadataPlant {
  plantId: string;
  name: string;
  quantities?: MetadataPlantQuantity[];
}
export interface MetadataPlantQuantity {
  amount?: number | null;
  unit?: string;
  ingredientName: string;
  ingredientPosition: number;
  kind?: "direct" | "shared" | "product";
}
export interface MetadataModelReview {
  provider: string;
  model: string;
}
export interface MetadataValidation {
  groupId: string;
  recipeId: string;
  authoringFingerprint: string;
  inputFingerprint: string;
  policyVersion: string;
}
export interface MetadataReuseOrigin {
  estimateId: string;
  inputFingerprint: string;
  inferredAt: string;
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
  nutritionMetadata?: RecipeNutritionMetadata | null;
  metadata?: RecipeGeneralMetadata | null;
}
export interface RecipeNutritionMetadata {
  schemaVersion?: 1;
  basis?: "per_serving";
  status: "source_backed" | "estimated" | "partial" | "unavailable";
  complete: boolean;
  nativeComplete: boolean;
  effectiveServings?: number | null;
  servingsQuality: "recorded" | "estimated" | "unavailable";
  perServing: NutritionMetadataFields;
  provenance?: NutritionProvenance[];
  inputFingerprint: string;
  calculatedAt: string;
}
export interface NutritionMetadataFields {
  kcal?: NutritionMetadataField;
  proteinG?: NutritionMetadataField;
  carbsG?: NutritionMetadataField;
  fatG?: NutritionMetadataField;
  fibreG?: NutritionMetadataField;
  waterMl?: NutritionMetadataField;
}
export interface NutritionMetadataField {
  verified?: number | null;
  estimated?: number | null;
  value?: number | null;
  quality?: "source_backed" | "estimated" | "unavailable";
  estimateMethods?: ("assumed_quantity" | "generic_food" | "source_substitution" | "model_estimate")[];
  provenanceIds?: string[];
  missingVerifiedIngredientRefs?: string[];
}
export interface NutritionProvenance {
  id: string;
  ingredientReference?: string | null;
  kind: "source" | "assumption" | "model_estimate";
  sourceFoodId?: string | null;
  sourceReleaseId?: string | null;
  sourceUrl?: string | null;
  assumption?: string | null;
  provider?: string | null;
  model?: string | null;
}
export interface RecipeGeneralMetadata {
  servings?: MetadataField;
  totalMinutes?: MetadataField;
  prepMinutes?: MetadataField;
  activeMinutes?: MetadataField;
  effort?: MetadataEffortField;
  source: MetadataSource;
  plants?: MetadataPlants;
  provenance?: NutritionProvenance[];
}
export interface MetadataField {
  recorded?: number | null;
  estimated?: number | null;
  value?: number | null;
  quality?: "recorded" | "derived" | "estimated" | "unavailable";
  provenanceIds?: string[];
}
export interface MetadataEffortField {
  recorded?: (1 | 2) | null;
  estimated?: (1 | 2) | null;
  value?: (1 | 2) | null;
  quality?: "recorded" | "derived" | "estimated" | "unavailable";
  provenanceIds?: string[];
}
export interface MetadataSource {
  ownership: "ephra" | "own";
  url?: string | null;
  attributionStatus: "recorded" | "not_recorded";
}
export interface MetadataPlants {
  recorded?: MetadataPlant[];
  estimated?: MetadataPlant[];
  status?: "complete" | "partial" | "unavailable";
  provenanceIds?: string[];
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
export interface MetadataIngredientResponse {
  referenceId: string;
  effectiveGrams: number;
  quantityAssumption?: string | null;
  sourceFoodId?: string | null;
  portionOrDensityId?: string | null;
  assumedPortionAmount?: number | null;
  nutrientsPer100g?: {
    [k: string]: number | null;
  };
  rationale: string;
}
export interface MetadataModel {}
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
export interface RecipeMetadataProviderResponse {
  effectiveServings: number;
  servingsAssumption?: string | null;
  ingredients: MetadataIngredientResponse[];
  generalMetadata: MetadataGeneralPlan;
}
export interface SourceDensityRow {
  sourceRecordId: string;
  name: string;
  gramsPerMl: number;
  referenceId: string;
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
