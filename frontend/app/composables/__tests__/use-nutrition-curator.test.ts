import { beforeEach, describe, expect, test, vi } from "vitest";
import type {
  HealthLabsFoodNutritionOut,
  HealthLabsNutrientFoodOut,
} from "~/lib/api/types/healthlabs";
import type { ApiRequestInstance } from "~/lib/api/types/non-generated";

const nutritionApi = vi.hoisted(() => ({
  getFoods: vi.fn(),
  searchSourceFoods: vi.fn(),
  setMatch: vi.fn(),
  clearMatch: vi.fn(),
  addPortion: vi.fn(),
}));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ healthlabsNutrition: nutritionApi }),
}));

const unmatchedFood: HealthLabsFoodNutritionOut = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Apple",
  status: "unknown",
  nutrition: null,
};

const candidate: HealthLabsNutrientFoodOut = {
  id: "22222222-2222-4222-8222-222222222222",
  sourceFoodId: "GB-APPLE-1",
  name: "Apple, raw",
  preparationState: "raw",
  nutrientsPer100g: {
    kcal: 52,
    proteinG: null,
    fibreG: 2.4,
    carbsG: 14,
    fatG: 0,
    waterMl: 86,
  },
  source: {
    id: "33333333-3333-4333-8333-333333333333",
    provider: "CoFID",
    version: "2026-01",
    country: "GB",
    sourceUrl: "https://example.test/source",
    licenseUrl: "https://example.test/license",
    attribution: "CoFID test data",
    sha256: "abc123",
    importedAt: "2026-01-01T00:00:00Z",
  },
};

const matchedFood: HealthLabsFoodNutritionOut = {
  ...unmatchedFood,
  status: "matched",
  nutrition: {
    foodId: unmatchedFood.id,
    nutrientFoodId: candidate.id,
    sourceFoodId: candidate.sourceFoodId,
    name: candidate.name,
    preparationState: candidate.preparationState,
    status: "matched",
    country: "GB",
    matchMethod: "manual",
    confidence: 1,
    reviewed: true,
    nutrientsPer100g: candidate.nutrientsPer100g,
    portions: [],
    source: candidate.source,
  },
};

const existingMatchedFood: HealthLabsFoodNutritionOut = {
  ...matchedFood,
  id: "55555555-5555-4555-8555-555555555555",
  name: "Pear",
  nutrition: {
    ...matchedFood.nutrition!,
    foodId: "55555555-5555-4555-8555-555555555555",
  },
};

const secondFood: HealthLabsFoodNutritionOut = {
  id: "66666666-6666-4666-8666-666666666666",
  name: "Banana",
  status: "unknown",
  nutrition: null,
};

const secondCandidate: HealthLabsNutrientFoodOut = {
  ...candidate,
  id: "77777777-7777-4777-8777-777777777777",
  sourceFoodId: "GB-BANANA-1",
  name: "Banana, raw",
};

beforeEach(() => {
  vi.clearAllMocks();
  nutritionApi.getFoods.mockResolvedValue({
    data: { items: [unmatchedFood, existingMatchedFood], total: 2 },
    error: null,
  });
  nutritionApi.searchSourceFoods.mockResolvedValue({ data: [candidate], error: null });
  nutritionApi.setMatch.mockResolvedValue({ data: matchedFood, error: null });
  nutritionApi.clearMatch.mockResolvedValue({ data: unmatchedFood, error: null });
  nutritionApi.addPortion.mockResolvedValue({
    data: {
      ...matchedFood,
      nutrition: {
        ...matchedFood.nutrition!,
        portions: [{ description: "medium", amount: 1, unit: "item", grams: 182 }],
      },
    },
    error: null,
  });
  vi.spyOn(crypto, "randomUUID").mockReturnValue("44444444-4444-4444-8444-444444444444");
});

describe("HealthLabsNutritionApi", () => {
  test("uses the nutrition routes and required audit headers", async () => {
    const requests = {
      get: vi.fn().mockResolvedValue({ data: null, error: null }),
      post: vi.fn().mockResolvedValue({ data: null, error: null }),
      put: vi.fn().mockResolvedValue({ data: null, error: null }),
      patch: vi.fn(),
      delete: vi.fn().mockResolvedValue({ data: null, error: null }),
    } as unknown as ApiRequestInstance;
    const { HealthLabsNutritionApi } = await import("~/lib/api/user/healthlabs-nutrition");
    const api = new HealthLabsNutritionApi(requests);

    await api.getFoods({ q: "apple", country: "GB" });
    await api.searchSourceFoods({ q: "raw apple", country: "GB", limit: 10 });
    await api.setMatch(unmatchedFood.id, { nutrientFoodId: candidate.id, country: "GB" }, "match-key");
    await api.clearMatch(unmatchedFood.id, "GB", "clear-key");
    await api.addPortion(
      unmatchedFood.id,
      { description: "medium", amount: 1, unit: "item", grams: 182 },
      "portion-key",
    );

    expect(requests.get).toHaveBeenNthCalledWith(
      1,
      "/api/healthlabs/v1/nutrition/foods?q=apple&country=GB",
    );
    expect(requests.get).toHaveBeenNthCalledWith(
      2,
      "/api/healthlabs/v1/nutrition/source-foods?q=raw+apple&country=GB&limit=10",
    );
    expect(requests.put).toHaveBeenCalledWith(
      `/api/healthlabs/v1/nutrition/foods/${unmatchedFood.id}/match`,
      { nutrientFoodId: candidate.id, country: "GB" },
      { headers: { "Idempotency-Key": "match-key", "X-HealthLabs-Access-Reason": "Nutrition curator source review" } },
    );
    expect(requests.delete).toHaveBeenCalledWith(
      `/api/healthlabs/v1/nutrition/foods/${unmatchedFood.id}/match?country=GB`,
      { headers: { "Idempotency-Key": "clear-key", "X-HealthLabs-Access-Reason": "Nutrition curator match removal" } },
    );
    expect(requests.post).toHaveBeenCalledWith(
      `/api/healthlabs/v1/nutrition/foods/${unmatchedFood.id}/portions`,
      { description: "medium", amount: 1, unit: "item", grams: 182 },
      { headers: { "Idempotency-Key": "portion-key", "X-HealthLabs-Access-Reason": "Nutrition curator measured portion" } },
    );
  });
});

describe("useNutritionCurator", () => {
  test("loads the queue, filters it, and keeps source nutrient nulls intact", async () => {
    const { useNutritionCurator } = await import("../use-nutrition-curator");
    const curator = useNutritionCurator();

    await curator.loadFoods();
    expect(nutritionApi.getFoods).toHaveBeenCalledWith({ country: "GB", q: undefined });
    expect(curator.visibleFoods.value).toEqual([unmatchedFood]);

    curator.queueFilter.value = "matched";
    expect(curator.visibleFoods.value).toEqual([existingMatchedFood]);

    await curator.selectFood(unmatchedFood);
    expect(nutritionApi.searchSourceFoods).toHaveBeenCalledWith({
      q: "Apple",
      country: "GB",
      limit: 20,
    });
    expect(curator.candidates.value[0]?.nutrientsPer100g.proteinG).toBeNull();
    expect(curator.candidates.value[0]?.nutrientsPer100g.fatG).toBe(0);
  });

  test("applies match and portion mutations without duplicating queue foods", async () => {
    const { useNutritionCurator } = await import("../use-nutrition-curator");
    const curator = useNutritionCurator();
    await curator.loadFoods();
    await curator.selectFood(unmatchedFood);

    await curator.approveSource(candidate);
    expect(nutritionApi.setMatch).toHaveBeenCalledWith(
      unmatchedFood.id,
      { nutrientFoodId: candidate.id, country: "GB", confidence: 1 },
      "44444444-4444-4444-8444-444444444444",
    );
    expect(curator.selectedFood.value).toEqual(matchedFood);
    expect(curator.foods.value).toHaveLength(2);

    await curator.addPortion({ description: "medium", amount: 1, unit: "item", grams: 182 });
    expect(curator.selectedFood.value?.nutrition?.portions).toEqual([
      { description: "medium", amount: 1, unit: "item", grams: 182 },
    ]);

    await curator.clearMatch();
    expect(nutritionApi.clearMatch).toHaveBeenCalledWith(
      unmatchedFood.id,
      "GB",
      "44444444-4444-4444-8444-444444444444",
    );
    expect(curator.selectedFood.value).toEqual(unmatchedFood);
  });

  test("preserves the current selection and candidates when a search fails", async () => {
    const { useNutritionCurator } = await import("../use-nutrition-curator");
    const curator = useNutritionCurator();
    await curator.selectFood(unmatchedFood);
    nutritionApi.searchSourceFoods.mockResolvedValueOnce({ data: null, error: new Error("offline") });
    curator.sourceQuery.value = "apple cooked";

    await curator.searchSources();

    expect(curator.selectedFood.value).toEqual(unmatchedFood);
    expect(curator.sourceQuery.value).toBe("apple cooked");
    expect(curator.candidates.value).toEqual([candidate]);
    expect(curator.sourceError.value).toBeTruthy();
  });

  test("discards a stale source response after another food is selected", async () => {
    let resolveFirst!: (value: unknown) => void;
    let resolveSecond!: (value: unknown) => void;
    nutritionApi.searchSourceFoods
      .mockReturnValueOnce(new Promise(resolve => resolveFirst = resolve))
      .mockReturnValueOnce(new Promise(resolve => resolveSecond = resolve));
    const { useNutritionCurator } = await import("../use-nutrition-curator");
    const curator = useNutritionCurator();

    const firstSelection = curator.selectFood(unmatchedFood);
    const secondSelection = curator.selectFood(secondFood);
    resolveSecond({ data: [secondCandidate], error: null });
    await secondSelection;
    resolveFirst({ data: [candidate], error: null });
    await firstSelection;

    expect(curator.selectedFood.value).toEqual(secondFood);
    expect(curator.candidates.value).toEqual([secondCandidate]);
  });
});
