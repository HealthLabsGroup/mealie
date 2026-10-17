import { flushPromises, mount } from "@vue/test-utils";
import { computed, nextTick, ref } from "vue";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { createVuetify } from "vuetify";
import * as components from "vuetify/components";
import * as directives from "vuetify/directives";
import type {
  HealthLabsFoodNutritionOut,
  HealthLabsNutrientFoodOut,
} from "~/lib/api/types/healthlabs";

const useNutritionCurator = vi.hoisted(() => vi.fn());

vi.mock("~/composables/use-nutrition-curator", () => ({ useNutritionCurator }));

const candidate: HealthLabsNutrientFoodOut = {
  id: "22222222-2222-4222-8222-222222222222",
  sourceFoodId: "GB-APPLE-1",
  name: "Apple, raw",
  preparationState: "raw",
  nutrientsPer100g: { kcal: 52, proteinG: null, fibreG: 2.4, carbsG: 14, fatG: 0 },
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
  id: "11111111-1111-4111-8111-111111111111",
  name: "Apple",
  status: "matched",
  nutrition: {
    foodId: "11111111-1111-4111-8111-111111111111",
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

const reviewFood: HealthLabsFoodNutritionOut = {
  id: "44444444-4444-4444-8444-444444444444",
  name: "Carrot",
  status: "review_required",
  nutrition: null,
};

const suggestedFood: HealthLabsFoodNutritionOut = {
  ...reviewFood,
  nutrition: {
    ...matchedFood.nutrition!,
    id: null,
    foodId: reviewFood.id,
    status: "review_required",
    reviewed: false,
    reviewedByUserId: null,
    reviewedAt: null,
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("definePageMeta", vi.fn());
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });

  const foods = ref([reviewFood, matchedFood]);
  const queueFilter = ref<"needs-review" | "matched" | "all">("needs-review");
  useNutritionCurator.mockReturnValue({
    foods,
    total: ref(2),
    visibleFoods: computed(() => foods.value.filter((food) => {
      if (queueFilter.value === "all") return true;
      if (queueFilter.value === "matched") return food.status === "matched";
      return food.status !== "matched";
    })),
    selectedFood: ref(matchedFood),
    candidates: ref([candidate]),
    country: ref("GB"),
    queueQuery: ref(""),
    sourceQuery: ref("Apple"),
    queueFilter,
    loadingFoods: ref(false),
    searchingSources: ref(false),
    saving: ref(false),
    foodError: ref(null),
    sourceError: ref(null),
    actionError: ref(null),
    loadFoods: vi.fn(),
    selectFood: vi.fn(),
    searchSources: vi.fn(),
    approveSource: vi.fn(),
    approveSuggestion: vi.fn(),
    clearMatch: vi.fn(),
    addPortion: vi.fn(),
  });
});

afterEach(() => vi.unstubAllGlobals());

async function mountPage() {
  const NutritionPage = (await import("./nutrition.vue")).default;
  return mount(NutritionPage, {
    global: {
      plugins: [createVuetify({ components, directives })],
      stubs: { Teleport: true },
    },
  });
}

test("renders the curator workflow and keeps unavailable nutrients distinct from zero", async () => {
  const wrapper = await mountPage();
  const curator = useNutritionCurator.mock.results[0]?.value;

  try {
    await flushPromises();
    expect(definePageMeta).toHaveBeenCalledWith({ middleware: ["auth", "nutrition-write-only"] });
    expect(curator.loadFoods).toHaveBeenCalledOnce();
    expect(wrapper.get("[data-testid='filter-needs-review']").text()).toContain("Needs review");
    expect(wrapper.get("[data-testid='filter-matched']").text()).toContain("Matched");
    expect(wrapper.get("[data-testid='filter-all']").text()).toContain("All");

    await wrapper.get("[data-testid='filter-matched']").trigger("click");
    expect(curator.queueFilter.value).toBe("matched");
    await wrapper.get("[data-testid='filter-all']").trigger("click");
    expect(curator.queueFilter.value).toBe("all");

    expect(wrapper.text()).toContain("CoFID");
    expect(wrapper.text()).toContain("2026-01");
    expect(wrapper.text()).toContain("GB");
    expect(wrapper.text()).toContain("GB-APPLE-1");
    expect(wrapper.text()).toContain("License");
    expect(wrapper.get("[data-testid='candidate-nutrient-protein']").text()).toContain("—");
    expect(wrapper.get("[data-testid='candidate-nutrient-fat']").text()).toContain("0 g");

    await wrapper.get("[data-testid='use-source']").trigger("click");
    expect(curator.approveSource).toHaveBeenCalledWith(candidate);

    expect(wrapper.text()).toContain("Description");
    expect(wrapper.text()).toContain("Amount");
    expect(wrapper.text()).toContain("Unit");
    expect(wrapper.text()).toContain("Grams");
    expect(wrapper.get("[data-testid='add-portion']").text()).toContain("Add portion");
  }
  finally {
    wrapper.unmount();
  }
});

test("shows an automatic suggestion without persisted-match controls", async () => {
  const curator = useNutritionCurator.mock.results[0]?.value ?? useNutritionCurator();
  curator.selectedFood.value = suggestedFood;
  useNutritionCurator.mockReturnValue(curator);
  const wrapper = await mountPage();

  try {
    await flushPromises();
    expect(wrapper.text()).toContain("Suggested source");
    expect(wrapper.find("[data-testid='clear-match']").exists()).toBe(false);
    expect(wrapper.find("[data-testid='add-portion']").exists()).toBe(false);
    await wrapper.get("[data-testid='use-suggestion']").trigger("click");
    expect(curator.approveSuggestion).toHaveBeenCalledOnce();
  }
  finally {
    wrapper.unmount();
  }
});

test("resets a measured portion draft after saving, changing food, or replacing its source", async () => {
  const wrapper = await mountPage();
  const curator = useNutritionCurator.mock.results[0]?.value;
  curator.addPortion.mockResolvedValue(true);

  try {
    await wrapper.get("[data-testid='portion-description'] input").setValue("medium");
    await wrapper.get("[data-testid='portion-amount'] input").setValue("1");
    await wrapper.get("[data-testid='portion-unit'] input").setValue("item");
    await wrapper.get("[data-testid='portion-grams'] input").setValue("182");
    await wrapper.get("[data-testid='portion-form']").trigger("submit");
    await flushPromises();
    expect(curator.addPortion).toHaveBeenCalledWith({
      description: "medium",
      amount: 1,
      unit: "item",
      grams: 182,
    });
    expect((wrapper.get("[data-testid='portion-description'] input").element as HTMLInputElement).value).toBe("");

    await wrapper.get("[data-testid='portion-description'] input").setValue("large");
    curator.selectedFood.value = { ...matchedFood, id: "88888888-8888-4888-8888-888888888888" };
    await nextTick();
    expect((wrapper.get("[data-testid='portion-description'] input").element as HTMLInputElement).value).toBe("");

    await wrapper.get("[data-testid='portion-description'] input").setValue("source-specific");
    const currentFood = curator.selectedFood.value;
    curator.selectedFood.value = {
      ...currentFood,
      nutrition: { ...currentFood.nutrition!, nutrientFoodId: candidate.id.replace(/^2/, "9") },
    };
    await nextTick();
    expect((wrapper.get("[data-testid='portion-description'] input").element as HTMLInputElement).value).toBe("");
  }
  finally {
    wrapper.unmount();
  }
});
